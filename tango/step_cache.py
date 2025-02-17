import collections
import logging
import weakref
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, MutableMapping, Optional, OrderedDict, TypeVar, Union

try:
    from typing import get_args, get_origin  # type: ignore
except ImportError:

    def get_origin(tp):  # type: ignore
        return getattr(tp, "__origin__", None)

    def get_args(tp):  # type: ignore
        return getattr(tp, "__args__", ())


from tango.common.aliases import PathOrStr
from tango.common.from_params import FromParams
from tango.common.registrable import Registrable
from tango.step import Step

logger = logging.getLogger(__name__)


T = TypeVar("T")


class StepCache(Registrable):
    """
    This is a mapping from instances of :class:`~tango.step.Step` to the results of that step.
    """

    default_implementation = "local"
    """
    The default implementation is :class:`LocalStepCache`.
    """

    def __contains__(self, step: Any) -> bool:
        """This is a generic implementation of ``__contains__``. If you are writing your own
        ``StepCache``, you might want to write a faster one yourself."""
        if not isinstance(step, Step):
            return False
        try:
            self.__getitem__(step)
            return True
        except KeyError:
            return False

    @abstractmethod
    def __getitem__(self, step: Step) -> Any:
        """Returns the results for the given step."""
        raise NotImplementedError()

    @abstractmethod
    def __setitem__(self, step: Step, value: Any) -> None:
        """Writes the results for the given step. Throws an exception if the step is already cached."""
        raise NotImplementedError()

    @abstractmethod
    def __len__(self) -> int:
        """Returns the number of results saved in this cache."""
        raise NotImplementedError()


@StepCache.register("memory")
class MemoryStepCache(StepCache):
    """
    This is a :class:`.StepCache` that stores results in memory. It is little more than a Python dictionary.

    .. tip::
        Registered as :class:`StepCache` under the name "memory".
    """

    def __init__(self):
        self.cache: Dict[str, Any] = {}

    def __getitem__(self, step: Step) -> Any:
        return self.cache[step.unique_id]

    def __setitem__(self, step: Step, value: Any) -> None:
        if step in self:
            raise ValueError(f"{step.unique_id} is already cached! Will not overwrite.")
        if step.cache_results:
            self.cache[step.unique_id] = value
        else:
            logger.warning("Tried to cache step %s despite being marked as uncacheable.", step.name)

    def __contains__(self, step: object):
        if isinstance(step, Step):
            return step.unique_id in self.cache
        else:
            return False

    def __len__(self) -> int:
        return len(self.cache)


default_step_cache = MemoryStepCache()


@StepCache.register("local")
class LocalStepCache(StepCache):
    """
    This is a :class:`StepCache` that stores its results on disk, in the location given in ``dir``.

    Every cached step gets a directory under ``dir`` with that step's :attr:`~tango.step.Step.unique_id`.
    In that directory we store the results themselves in some format according to the step's
    :attr:`~tango.step.Step.FORMAT`, and we also write a ``cache-metadata.json`` file that
    stores the :class:`CacheMetadata`.

    The presence of ``cache-metadata.json`` signifies that the cache entry is complete and
    has been written successfully.

    .. tip::
        Registered as :class:`StepCache` under the name "local".

    """

    LRU_CACHE_MAX_SIZE = 8

    def __init__(self, dir: PathOrStr):
        self.dir = Path(dir)
        self.dir.mkdir(parents=True, exist_ok=True)

        # We keep an in-memory cache as well so we don't have to de-serialize stuff
        # we happen to have in memory already.
        self.weak_cache: MutableMapping[str, Any] = weakref.WeakValueDictionary()

        # Not all Python objects can be referenced weakly, and even if they can they
        # might get removed too quickly, so we also keep an LRU cache.
        self.strong_cache: OrderedDict[str, Any] = collections.OrderedDict()

    def _add_to_cache(self, key: str, o: Any) -> None:
        if hasattr(o, "__next__"):
            # We never cache iterators, because they are mutable, storing their current position.
            return

        self.strong_cache[key] = o
        self.strong_cache.move_to_end(key)
        while len(self.strong_cache) > self.LRU_CACHE_MAX_SIZE:
            del self.strong_cache[next(iter(self.strong_cache))]

        try:
            self.weak_cache[key] = o
        except TypeError:
            pass  # Many native Python objects cannot be referenced weakly, and they throw TypeError when you try

    def _get_from_cache(self, key: str) -> Optional[Any]:
        result = self.strong_cache.get(key)
        if result is not None:
            self.strong_cache.move_to_end(key)
            return result
        try:
            return self.weak_cache[key]
        except KeyError:
            return None

    def __contains__(self, step: object) -> bool:
        if isinstance(step, Step) and step.cache_results:
            key = step.unique_id
            if key in self.strong_cache:
                return True
            if key in self.weak_cache:
                return True
            metadata_file = self.step_dir(step) / "cache-metadata.json"
            return metadata_file.exists()
        else:
            return False

    def __getitem__(self, step: Step) -> Any:
        key = step.unique_id
        result = self._get_from_cache(key)
        if result is None:
            if step not in self:
                raise KeyError(step)
            result = step.format.read(self.step_dir(step))
            self._add_to_cache(key, result)
        return result

    def __setitem__(self, step: Step, value: Any) -> None:
        if not step.cache_results:
            logger.warning("Tried to cache step %s despite being marked as uncacheable.", step.name)
            return

        location = self.step_dir(step)
        location.mkdir(parents=True, exist_ok=True)

        metadata_location = location / "cache-metadata.json"
        if metadata_location.exists():
            raise ValueError(f"{metadata_location} already exists! Will not overwrite.")
        temp_metadata_location = metadata_location.with_suffix(".temp")

        try:
            step.format.write(value, location)
            metadata = CacheMetadata(step=step.unique_id)
            metadata.to_params().to_file(temp_metadata_location)
            self._add_to_cache(step.unique_id, value)
            temp_metadata_location.rename(metadata_location)
        except:  # noqa: E722
            try:
                temp_metadata_location.unlink()
            except FileNotFoundError:
                pass
            raise

    def __len__(self) -> int:
        return sum(1 for _ in self.dir.glob("*/cache-metadata.json"))

    def step_dir(self, step_or_unique_id: Union[Step, str]) -> Path:
        """Returns the directory that contains the results of the step.

        You can use this even for a step that's not cached yet. In that case it will return the directory where
        the results will be written."""
        if isinstance(step_or_unique_id, Step):
            if not step_or_unique_id.cache_results:
                raise RuntimeError(
                    f"Uncacheable steps (like '{step_or_unique_id.name}') don't have step directories."
                )
            unique_id = step_or_unique_id.unique_id
        else:
            unique_id = step_or_unique_id
        return self.dir / unique_id


@dataclass
class CacheMetadata(FromParams):
    step: str
    """
    The step name.
    """
