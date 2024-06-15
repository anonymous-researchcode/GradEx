import argparse
import logging
import os

from src.custom.data_module import DataModule
from src.data.completion_dataset import CompletionMetadata

os.environ["TOKENIZERS_PARALLELISM"] = "false"

import pytorch_lightning as pl
import torch
from transformers import T5TokenizerFast, T5ForConditionalGeneration
from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
from peft import get_peft_model, LoraConfig

from src.custom.model import Model
from torch.utils.data import DataLoader
import numpy as np

logging.basicConfig(level=logging.INFO)
torch.set_float32_matmul_precision("high")

def get_trainable_parameters(model):
    return (p for p in model.parameters() if p.requires_grad)

def main(args):
    args.enable_checkpointing = not args.disable_checkpointing
    print("arguments".upper().center(80, "-"))
    print(args)
    print("-" * 80)

    if args.precision == 16:
        args.precision = "bf16"
        print("Setting precision to bf16")

    dataset_key = args.dataset_key
    model_key = args.model_key
    train_key = args.train_key

    if "flan" in model_key:
        hf_key = "google/{}".format(model_key.replace("_", "-"))
        model = AutoModelForSeq2SeqLM.from_pretrained(hf_key)
        tokenizer = AutoTokenizer.from_pretrained(hf_key, model_max_length=512)
        model_type = "encoder_decoder"
        append_eos = False  # t5 tokenizers already append eos
    elif "t5" in model_key:
        hf_key = model_key.replace("_", "-")
        model = T5ForConditionalGeneration.from_pretrained(hf_key)
        tokenizer = T5TokenizerFast.from_pretrained(hf_key, model_max_length=512)
        model_type = "encoder_decoder"
        append_eos = False
    elif "gpt2" in model_key:
        from transformers import GPT2Tokenizer, GPT2LMHeadModel

        hf_key = model_key.replace("_", "-")
        tokenizer = GPT2Tokenizer.from_pretrained(hf_key)
        model = GPT2LMHeadModel.from_pretrained(hf_key)
        model_type = "decoder"
        append_eos = True
    else:
        raise NotImplementedError(model_key)

    if args.train_lora:
        config = LoraConfig(
            r=args.lora_rank,
            lora_alpha=args.lora_alpha,
            target_modules=["q", "k", "v"],
            lora_dropout=0.1,
            bias="lora_only",
            modules_to_save=[],
        )
        model = get_peft_model(model, config)
        model.print_trainable_parameters()


    if "ft_cot" in args.preset_key:
        completion_key = "ft_cot"
    elif args.preset_key == "ft":
        completion_key = "ft"
    elif args.preset_key == "fs_cot":
        raise NotImplementedError("We don't train models on fs_cot")
    else:
        raise NotImplementedError(args.preset_key)

    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    batch_size = args.batch_size
    if args.inference_batch_size is None:
        inference_batch_size = batch_size
    else:
        inference_batch_size = args.inference_batch_size
    data_module = DataModule(dataset_key, args.preset_key, tokenizer, model_type, batch_size=batch_size,
                                inference_batch_size=inference_batch_size, num_workers=8, append_eos=append_eos)

    data_module.setup("fit")
    train_loader = DataLoader(
                data_module.train_dataset,
                batch_size=data_module.batch_size,
                num_workers=data_module.num_workers,
                shuffle=False)
    test_loader = DataLoader(
                data_module.test_dataset,
                batch_size=data_module.batch_size,
                num_workers=data_module.num_workers,
                shuffle=False)

    cm = CompletionMetadata(model_key, completion_key, dataset_key, prediction_template=data_module.prediction_template)
    lm = Model(model, tokenizer, model_type, completion_metadata=cm, truncate_early=False)
    load_model_dir = args.load_model_dir

    if load_model_dir is not None:
        load_model_dir = os.path.join("external_lightning_logs", load_model_dir)
        lm = lm.load_from_checkpoint(load_model_dir + ".ckpt", model=model, tokenizer=tokenizer, model_type=model_type)

    device = torch.device(f"cuda:{args.devices[0]}")


    gradients_dir = f"./gradients/{args.dataset_key}_{args.model_key}_{args.preset_key}_{args.project_dim}/run_{args.run}" if args.load_model_dir is not None else \
        f"./gradients/{args.dataset_key}_{args.model_key}_{args.preset_key}_{args.project_dim}_pretrained/run_{args.run}"

    if not os.path.exists(gradients_dir):
        os.makedirs(gradients_dir)

    gradient_dim = 0
    for name, param in model.named_parameters():
        if param.requires_grad:
            gradient_dim += param.numel()

    # if args.create_projection:
    #     if os.path.exists(f"./gradients/{args.dataset_key}_{args.model_key}_{args.preset_key}_{args.project_dim}/projection_matrix_{args.run}.npy"):
    #         print("Loading projection matrix")
    #         matrix_P = np.load(f"./gradients/{args.dataset_key}_{args.model_key}_{args.preset_key}_{args.project_dim}/projection_matrix_{args.run}.npy")
    #     else:
    np.random.seed(args.run)
    project_dim = args.project_dim
    project_matrix = (2 * np.random.randint(2, size=(gradient_dim, project_dim)) - 1).astype(float)
    project_matrix *= 1 / np.sqrt(project_dim)
    # np.save(f"./gradients/{args.dataset_key}_{args.model_key}_{args.preset_key}_{args.project_dim}/projection_matrix_{args.run}.npy", matrix_P)
    # else:
    #     idx = args.run % 10
    #     print("Loading projection matrix: ", idx)
    #     matrix_P = np.load(f"./gradients/{args.dataset_key}_{args.model_key}_{args.preset_key}_{args.project_dim}/projection_matrix_{idx}.npy")

    lm = lm.to(device)
    lm.model.eval()
    # Save gradients
    for batch_idx, batch in enumerate(train_loader):
        batch = {k: v.to(lm.device) for k, v in batch.items()}
        # output = lm.training_step(batch, 0)
        
        kwargs = {
            "input_ids": batch["input_ids"],
            "attention_mask": batch["attention_mask"],
            "labels": batch["labels"],
        }
        if lm.model_type == "encoder_decoder":
            kwargs["decoder_attention_mask"] = batch["decoder_attention_mask"]
        logits = lm.model(**kwargs)["logits"]

        # get the gradient of the output
        labels = kwargs["labels"]
        gradients = []
        for i in range(len(labels)):
            tmp_mask = labels[i] != -100
            tmp_logits = logits[i][tmp_mask]
            tmp_probs = torch.softmax(tmp_logits, dim=-1)
            tmp_labels = labels[i][tmp_mask]

            tmp_outputs = tmp_probs[range(tmp_probs.size(0)), tmp_labels] - 1e-3
            tmp_outputs = torch.log(tmp_outputs/(1-tmp_outputs+1e-10))
            tmp_loss = tmp_outputs.mean()

            tmp_gradients = torch.autograd.grad(tmp_loss, get_trainable_parameters(lm.model), retain_graph=True, create_graph=False)
            tmp_gradients = torch.cat([gradient.view(-1) for gradient in tmp_gradients]).cpu().numpy() # flatten gradients
            tmp_gradients = (tmp_gradients.reshape(1, -1) @ project_matrix).flatten()
            gradients.append(tmp_gradients)

        np.save(f"{gradients_dir}/train_batch_{batch_idx}_gradients.npy", gradients)

    for batch_idx, batch in enumerate(test_loader):
        batch = {k: v.to(lm.device) for k, v in batch.items()}
        
        kwargs = {
            "input_ids": batch["input_ids"],
            "attention_mask": batch["attention_mask"],
            "labels": batch["labels"],
        }
        if lm.model_type == "encoder_decoder":
            kwargs["decoder_attention_mask"] = batch["decoder_attention_mask"]
        logits = lm.model(**kwargs)["logits"]

        # get the gradient of the output
        labels = kwargs["labels"]
        gradients = []
        for i in range(len(labels)):
            tmp_mask = labels[i] != -100
            tmp_logits = logits[i][tmp_mask]
            tmp_probs = torch.softmax(tmp_logits, dim=-1)
            tmp_labels = labels[i][tmp_mask]

            tmp_outputs = tmp_probs[range(tmp_probs.size(0)), tmp_labels] - 1e-3
            tmp_outputs = torch.log(tmp_outputs/(1-tmp_outputs+1e-10))
            tmp_loss = tmp_outputs.mean()

            tmp_gradients = torch.autograd.grad(tmp_loss, get_trainable_parameters(lm.model), retain_graph=True, create_graph=False)
            tmp_gradients = torch.cat([gradient.view(-1) for gradient in tmp_gradients]).cpu().numpy() # flatten gradients
            tmp_gradients = (tmp_gradients.reshape(1, -1) @ project_matrix).flatten()
            gradients.append(tmp_gradients)

        np.save(f"{gradients_dir}/test_batch_{batch_idx}_gradients.npy", gradients)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset_key", type=str, default="multiarith")
    parser.add_argument("--model_key", type=str, default="flan_t5_base")
    parser.add_argument("--train_key", type=str, default="ft_cot")
    parser.add_argument("--batch_size", type=int, default=8)
    parser.add_argument("--preset_key", type=str, default="ft_cot_t70_64aug")
    parser.add_argument("--inference_batch_size", type=int, default=None)
    parser.add_argument("--devices", type=int, nargs="+", default=[0])
    parser.add_argument("--accumulate", type=int, default=1)
    parser.add_argument("--strategy", type=str, default=None)
    parser.add_argument("--precision", type=int, default=32)
    parser.add_argument("--lr", type=float, default=3e-4)
    parser.add_argument("--disable_checkpointing", action="store_true")

    parser.add_argument("--load_model_dir", type=str, default=None)

    # projections
    parser.add_argument("--create_projection", action="store_true")
    parser.add_argument("--project_dim", type=int, default=200)
    parser.add_argument("--run", type=int, default=0)

    parser.add_argument("--train_lora", action="store_true")
    parser.add_argument("--lora_rank", type=int, default=4)
    parser.add_argument("--lora_alpha", type=int, default=32)

    args = parser.parse_args()
    main(args)