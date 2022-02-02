local pretrained_model = "sshleifer/tiny-gpt2";

####################
# Trainer settings #
####################

local training_steps = 4;
local validate_every = 4;

local devices = 2;
local grad_accum = 1;
local batch_size = 2;

local activation_checkpointing = true;
local amp = false;
local fsdp = true;
local cpu_offloading = false;  # Can only be used with 'fsdp' - saves a lot of GPU memory by offloading params+gradients to CPU, but is very slow.

######################
# Optimizer settings #
######################

local warmup_steps = 2;
local learning_rate = 0.005;


local fsdp_config = {
    reshard_after_forward: true,
    move_params_to_cpu: cpu_offloading,
    move_grads_to_cpu: cpu_offloading,
    mixed_precision: amp,
};

local training_engine = {
    type: "fairscale",
    amp: amp,
    fsdp_config: fsdp_config,
};

local dataloader = {
  batch_size: batch_size,
  collate_fn: { type: "transformers::DefaultDataCollator" },
  sampler: {
    type: "torch::DistributedSampler",
    shuffle: true,
    drop_last: true,
  },
};

{
    steps: {
        tokenized_data: {
            type: "fairscale_test_load_data",
            tokenizer: { pretrained_model_name_or_path: pretrained_model }
        },
        trained_model: {
            type: "torch::train",
            model: {
                type: "fairscale::with_wrapped_modules",
                model: {
                    type: "transformers::AutoModelForCausalLM::from_pretrained",
                    pretrained_model_name_or_path: pretrained_model,
                },
                modules_to_wrap: ["transformer\\.h\\.[0-9]+"],  # tell FairScale to wrap the transformer's blocks individually
                fsdp_config: fsdp_config,
                activation_checkpointing: activation_checkpointing,
            },
            dataset_dict: { type: "ref", ref: "tokenized_data" },
            train_dataloader: dataloader,
            validation_split: "dev",
            optimizer: {
                type: "torch::AdamW",
                lr: learning_rate,
                betas: [0.9, 0.95],
                eps: 1e-6,
            },
            lr_scheduler: {
                type: "transformers::linear",
                num_warmup_steps: warmup_steps,
                num_training_steps: training_steps,
            },
            grad_accum: grad_accum,
            train_steps: training_steps,
            validate_every: training_steps,
            validation_steps: 2,
            checkpoint_every: training_steps,
            log_every: 1,
            device_count: devices,
            training_engine: training_engine,
        },
    }
}
