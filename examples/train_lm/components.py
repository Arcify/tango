from typing import Any, Dict, List, Optional

import datasets
import torch
from transformers import (
    AutoConfig,
    AutoModelForCausalLM,
    AutoTokenizer,
    default_data_collator,
)

from tango import Step
from tango.integrations.datasets import DatasetsFormat
from tango.integrations.fairscale import FSDPConfig
from tango.integrations.torch import DataCollator, Model


# Normally we register classes directly, but here it's more convenient to register our own little
# factory function that will initialize a model with `AutoModelForCausalLM.from_pretrained` and then
# wrap the layers with FairScale's `FullyShardedDataParallel` and `checkpoint_wrapper()` if needed.
@Model.register("lm_pretrained")  # type: ignore
def from_pretrained(
    pretrained_model_name_or_path: str,
    *args,
    fsdp_config: Optional[FSDPConfig] = None,
    activation_checkpointing: bool = False,
    **kwargs,
) -> Model:
    model = AutoModelForCausalLM.from_pretrained(pretrained_model_name_or_path, *args, **kwargs)
    _fairscale_wrap_layers(
        model,
        fsdp_config=fsdp_config,
        activation_checkpointing=activation_checkpointing,
    )
    return model


# Same thing as above, except this won't load the pretrained state dictionary, so you get a randomly initialized
# model that you can train from scratch.
@Model.register("lm_fresh", exist_ok=True)  # type: ignore
def new_random_from_config(
    pretrained_model_name_or_path: str,
    fsdp_config: Optional[FSDPConfig] = None,
    activation_checkpointing: bool = False,
) -> Model:
    assert isinstance(fsdp_config, FSDPConfig)
    config = AutoConfig.from_pretrained(pretrained_model_name_or_path)
    model = AutoModelForCausalLM.from_config(config)  # type: ignore
    _fairscale_wrap_layers(
        model,
        fsdp_config=fsdp_config,
        activation_checkpointing=activation_checkpointing,
    )
    return model


def _fairscale_wrap_layers(
    model,
    fsdp_config: Optional[FSDPConfig] = None,
    activation_checkpointing: bool = False,
) -> None:
    if activation_checkpointing:
        from fairscale.nn.checkpoint import checkpoint_wrapper

        for block_idx in range(len(model.transformer.h)):
            model.transformer.h[block_idx] = checkpoint_wrapper(
                model.transformer.h[block_idx], offload_to_cpu=True
            )

    if fsdp_config is not None and torch.distributed.is_initialized():
        for block_idx in range(len(model.transformer.h)):
            model.transformer.h[block_idx] = fsdp_config.wrap(model.transformer.h[block_idx])


# And we also want to use the `default_data_collator()` function from HF as a `DataCollator`,
# so we create simple wrapper class around that function and register it.
@DataCollator.register("transformers_default")
class TransformerDefaultCollator(DataCollator[Any]):
    def __call__(self, items: List[Any]) -> Dict[str, Any]:
        return default_data_collator(items)


# Lastly, we need a step to tokenize the raw data. The result of this step will be passed
# directly into the "torch::train" step.
@Step.register("tokenize_data")
class TokenizeData(Step):
    DETERMINISTIC = True
    CACHEABLE = True
    FORMAT = DatasetsFormat()

    def run(  # type: ignore[override]
        self,
        dataset: datasets.DatasetDict,
        pretrained_model_name: str,
        block_size: int = 1024,
        num_workers: int = 1,
    ) -> datasets.DatasetDict:
        tokenizer = AutoTokenizer.from_pretrained(pretrained_model_name)

        def tokenize_function(example):
            return tokenizer(example["text"])

        dataset = dataset.map(
            tokenize_function,
            batched=True,
            num_proc=num_workers,
            remove_columns=["text"],
            desc="Tokenizing dataset",
            cache_file_names={
                "train": f"/tmp/wikitext2-train-{pretrained_model_name.replace('/', '-')}-tokenized.cache",
                "test": f"/tmp/wikitext2-test-{pretrained_model_name.replace('/', '-')}-tokenized.cache",
                "validation": f"/tmp/wikitext2-dev-{pretrained_model_name.replace('/', '-')}-tokenized.cache",
            },
        )

        def group_texts(examples):
            # Concatenate all texts.
            concatenated_examples = {k: sum(examples[k], []) for k in examples.keys()}  # type: ignore
            total_length = len(concatenated_examples[list(examples.keys())[0]])
            # We drop the small remainder, we could add padding if the model supported
            # it instead of this drop, you can customize this part to your needs.
            if total_length >= block_size:
                total_length = (total_length // block_size) * block_size
            # Split by chunks of max_len.
            result = {
                k: [t[i : i + block_size] for i in range(0, total_length, block_size)]
                for k, t in concatenated_examples.items()
            }
            result["labels"] = result["input_ids"].copy()
            return result

        dataset = dataset.map(
            group_texts,
            batched=True,
            num_proc=num_workers,
            desc=f"Grouping texts into chunks of {block_size}",
            cache_file_names={
                "train": f"/tmp/wikitext2-train-{pretrained_model_name.replace('/', '-')}-chunked.cache",
                "test": f"/tmp/wikitext2-test-{pretrained_model_name.replace('/', '-')}-chunked.cache",
                "validation": f"/tmp/wikitext2-dev-{pretrained_model_name.replace('/', '-')}-chunked.cache",
            },
        )

        return dataset