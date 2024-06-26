
WANDB_MODE=online CUDA_VISIBLE_DEVICES=6 \
torchrun --nproc_per_node=1 --master_port=26712 \
train_mixtral_pattern_predictor.py \
   --model_name_or_path mistralai/Mistral-7B-Instruct-v0.2 \
    --output_dir ./logs/ \
    --bf16 True \
    --tf32 True \
    --evaluation_strategy "epoch" \
    --lazy_preprocess True \
    --save_strategy "epoch" \
    --save_steps 500 \
    --save_total_limit 1 \
    --logging_steps 20 \
    --num_train_epochs 50 \
    --load_best_model_at_end True \
    --per_device_train_batch_size 8 \
    --per_device_eval_batch_size 8 \
    --gradient_accumulation_steps 2 \
    --learning_rate 4e-5 \
    --weight_decay 5e-3 \
    --warmup_ratio 0.03 \
    --lr_scheduler_type "cosine" \
    $@
   #  --sharded_ddp "simple" \
