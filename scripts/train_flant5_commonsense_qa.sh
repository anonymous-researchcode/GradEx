python custom_train.py --dataset_key commonsense_qa --model_key flan_t5_base --train_key ft_cot \
    --preset_key ft_cot --devices 0 --batch_size 8 --inference_batch_size 32 \
    --train_lora --lora_rank 16 --lora_alpha 128 --runs 2 --save_name new