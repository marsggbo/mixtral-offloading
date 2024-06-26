import sys
from typing import List, Optional, Tuple, Union, Dict
import time
import os
import torch
import json
from types import SimpleNamespace
import numpy as np
from transformers import AutoConfig, AutoTokenizer
from datasets import load_dataset, Dataset
from transformers import TextStreamer

from hqq.core.quantize import BaseQuantizeConfig
from offloadMoE.build_model import OffloadConfig, QuantConfig, build_model
from offloadMoE.custom_layers import SparseMoeWrapper
from offloadMoE.modeling_mixtral import build_offload_model


def load_json(file):
    with open(file, 'r') as f:
        data = json.load(f)
    return data

def prepare_data(dataset_list: Dict[str,int]):
    data = []
    # alpaca_data
    if 'alpaca' in dataset_list:
        alpaca_data = load_json("/home/nus-hx/code/Sequence-Scheduling/data/alpaca-train-10k.json")
        num_samples = dataset_list['alpaca']
        for i in range(num_samples):
            data.append(alpaca_data[i]['conversations'][0]['value'])

    # sst2
    if 'sst2' in dataset_list:
        sst2_data = load_dataset("stanfordnlp/sst2")['train'] # contain 67349 samples
        prefix_for_sst2 = '''For each given sentence, determine the sentiment expressed. If the sentiment is positive, return "positive". If the sentiment is negative, return "negative". Consider only these two categories for sentiment analysis. Please analyze the sentiment of the following sentence:'''
        num_samples = dataset_list['sst2']
        for i in range(num_samples):
            data.append(prefix_for_sst2 + sst2_data[i]['sentence'])

    # mrpc
    if 'mrpc' in dataset_list:
        mrpc_data  = load_dataset("SetFit/mrpc")["train"] # contain 3668 samples
        prefix_for_mrpc = '''Given two sentences, determine whether they express the same meaning. If they are paraphrases of each other, return "equivalent". If they are not, return "not equivalent". Please evaluate the following sentence pair:\n
        Sentence 1: "{}"
        Sentence 2: "{}"'''
        num_samples = dataset_list['mrpc']
        for i in range(num_samples):
            sample = mrpc_data[i]
            data.append(prefix_for_mrpc.format(sample['text1'], sample['text2']))

    # # yizhongw/self_instruct
    if 'yizhongw' in dataset_list:
        dataset = load_dataset("yizhongw/self_instruct", "super_natural_instructions")
        data_prompts = dataset['train']['prompt']
        num_samples = dataset_list['yizhongw']
        for i in range(num_samples):
            data.append(data_prompts[i])

    if 'tick666-math' in dataset_list:
        dataset = load_dataset("TICK666/Basic-Math-Chinese-1M-V1.1")['train'] # contains 1000000 samples
        num_samples = dataset_list['tick666-math']
        for i in range(num_samples):
            data.append(dataset[i]['text'])
    print(f"The data contains {len(data)} samples.")
    return data

def prepare_model(
    device,
    buffer_size=4,
    offload_per_layer = 4
):
    print(f'Building and Loading a MoE model...')
    quantized_model_name = "lavawolfiee/Mixtral-8x7B-Instruct-v0.1-offloading-demo"
    state_path = "/home/nus-hx/code/offloadMoE/data"

    config = AutoConfig.from_pretrained(quantized_model_name)
    ##### Change this to 5 if you have only 12 GB of GPU VRAM #####
    # offload_per_layer = 5
    ###############################################################

    num_experts = config.num_local_experts
    offload_config = OffloadConfig(
        main_size=config.num_hidden_layers * (num_experts - offload_per_layer),
        offload_size=config.num_hidden_layers * offload_per_layer,
        buffer_size=buffer_size,
        offload_per_layer=offload_per_layer,
    )

    attn_config = BaseQuantizeConfig(
        nbits=4,
        group_size=64,
        quant_zero=True,
        quant_scale=True,
    )
    attn_config["scale_quant_params"]["group_size"] = 256
    ffn_config = BaseQuantizeConfig(
        nbits=2,
        group_size=16,
        quant_zero=True,
        quant_scale=True,
    )
    quant_config = QuantConfig(ffn_config=ffn_config, attn_config=attn_config)

    model = build_model(
        device=device,
        quant_config=quant_config,
        offload_config=offload_config,
        state_path=state_path,
    )
    return model
    


def main(args):
    if args.ipdb:
        from ipdb import set_trace
        set_trace()
    dataset_list = {
        'alpaca': 5000,
        # 'sst2': 1000,
        # 'mrpc': 1000,
        # 'tick666-math': 1000,
        # 'yizhongw': 1000
    }
    print(f'Building dataset including {dataset_list}')
    data = prepare_data(dataset_list)
    ###### random order
    # indices = list(range(len(data)))
    # np.random.shuffle(indices)
    # data = np.array(data)[indices]
    ###### length-sorted order
    data = np.array(sorted(data, key=len))
    batch_size = 16
    batches = [data[i:i + batch_size] for i in range(0, len(data), batch_size)]

    device = torch.device("cuda:0")
    model = prepare_model(device, offload_per_layer=4, buffer_size=4)
    # model = build_offload_model(offload_per_layer=4, buffer_size=4)
    model = model.to(device)
    model_name = "mistralai/Mixtral-8x7B-Instruct-v0.1"
    max_new_tokens = 32
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = 'left'

    ###### baseline: original implementation
    if args.task == 0:
        run_benchmark(model, tokenizer, batches, max_new_tokens, device)

    ###### get pattern matrices of given batches of requests, including prefilling and decoding tokens
    elif args.task ==1:
        pattern_matrices = get_pattern_matrices(model, tokenizer, batches, max_new_tokens, device)

    ###### Idea: run with ground truth of pattern matrices
    elif args.task == 2:
        pattern_matrices = torch.load(args.pattern_matrices_path)
        run_benchmark_with_patterns(model, tokenizer, batch_size, max_new_tokens, device, pattern_matrices)

    elif args.task == 3:
        predictor = ...
        run_benchmark_with_predictor(model, tokenizer, batches, max_new_tokens, device, predictor)

    else:
        raise NotImplementedError

def run_benchmark(model, tokenizer, batches, max_new_tokens, device):
    num_tokens = []
    torch.cuda.synchronize()
    start = time.time()
    for batch_idx, batch in enumerate(batches):
        batch = batch.tolist()
        data = tokenizer(batch, return_tensors="pt", return_attention_mask=True, padding=True)
        data = {key: val.to(device) for key, val in data.items()}
        num_tokens.append(data['input_ids'].numel())
        batch_start = time.time()
        generated_token_ids, router_logits = custom_generate(
            data['input_ids'], data['attention_mask'], model, max_new_tokens=max_new_tokens, predictor=None
        )
        batch_end = time.time()
    torch.cuda.synchronize()
    end = time.time()
    total_num_tokens = np.sum(num_tokens)
    throughput = total_num_tokens / (end - start)
    print(f"Throughput: {total_num_tokens} tokens/{end-start} sec = {throughput} tokens/s")


def get_pattern_matrices(model, tokenizer, batches, max_new_tokens, device):
    # Initialize a dictionary to hold the activations
    pattern_matrices = {
        # 0: {
        #     'prompt_text': "this is a prompt",
        #     'prompt_ids': [], # prompt token list
        #     'prompt_pattern': , # 大小为(seq_len, num_layers, num_experts)的 one-hot 矩阵
        #     'decode_ids': [], # deocde token list
        #     'decode_pattern': , # 大小为(seq_len, num_layers, num_experts)的 one-hot 矩阵
        # },
        # 1: {},
        # ...
    }
    for batch_idx, batch in enumerate(batches):
        batch = batch.tolist()
        data = tokenizer(batch, return_tensors="pt", return_attention_mask=True, padding=True)
        data = {key: val.to(device) for key, val in data.items()}
        generated_token_ids, router_logits = custom_generate(
            data['input_ids'], data['attention_mask'], model, max_new_tokens=max_new_tokens, predictor=None
        )
        bs, seq_len = generated_token_ids.shape
        num_layers = len(router_logits)
        token_pattern_matrices_logits = torch.stack(router_logits, dim=-2) # (num_tokens, num_layers, num_experts)
        token_pattern_matrices_logits = token_pattern_matrices_logits.view(bs, seq_len-1, num_layers, -1) # (bs, seq_len, num_layers, num_experts)
        token_router_indices = token_pattern_matrices_logits.topk(2, dim=-1)[1].cpu()
        token_router_indices = token_router_indices.permute(0, 2, 1, 3) # (batch_size, num_layers, seq_len, top2_indices)

        for i, text in enumerate(batch):
            prompt_ids = data['input_ids'][i].cpu()[data['attention_mask'][i].cpu()==1]
            decode_ids = generated_token_ids[i].detach().cpu()[-1*max_new_tokens:]
            pad_len = (data['attention_mask'][i]==0).sum().item()
            decode_start_idx = data['input_ids'][i].shape[0]
            pattern_matrices[len(batch)*batch_idx+i] = {
                'prompt_text': text,
                'prompt_ids': prompt_ids,
                'decode_ids': decode_ids,
                'prompt_pattern': token_router_indices[i, :, pad_len:decode_start_idx].tolist(),
                'decode_pattern': token_router_indices[i, :, -1*max_new_tokens:].tolist()
            }
    torch.save(pattern_matrices, 'pattern_matrices.pt')
    hf_pattern_matrices = {
        'prompt_text': [],
        'prompt_ids': [],
        'decode_ids': [],
        'prompt_pattern': [],
        'decode_pattern': []
    }
    for i in range(len(pattern_matrices)):
        hf_pattern_matrices['prompt_text'].append(pattern_matrices[i]['prompt_text'])
        hf_pattern_matrices['prompt_ids'].append(pattern_matrices[i]['prompt_ids'])
        hf_pattern_matrices['decode_ids'].append(pattern_matrices[i]['decode_ids'])
        hf_pattern_matrices['prompt_pattern'].append(pattern_matrices[i]['prompt_pattern'])
        hf_pattern_matrices['decode_pattern'].append(pattern_matrices[i]['decode_pattern'])
    hf_pattern_matrices_dataset = Dataset.from_dict(hf_pattern_matrices)
    hf_pattern_matrices_dataset.push_to_hub(f'marsggbo/mixtral8x7b_quant_alpaca5k_pattern')    
    return pattern_matrices


def run_benchmark_with_predictor(model, tokenizer, batches, max_new_tokens, device, predictor):
    # Initialize a dictionary to hold the activations
    num_tokens = []
    torch.cuda.synchronize()
    start = time.time()
    
    for batch_idx, batch in enumerate(batches):
        batch = batch.tolist()
        data = tokenizer(batch, return_tensors="pt", return_attention_mask=True, padding=True)
        data = {key: val.to(device) for key, val in data.items()}
        num_tokens.append(data['input_ids'].numel())
        batch_start = time.time()
        generated_token_ids, router_logits = custom_generate(
            data['input_ids'], data['attention_mask'], model, max_new_tokens=max_new_tokens, predictor=None
        )
        batch_end = time.time()
        print(f"Processing batch {batch_idx} data.input_ids.shape={data['input_ids'].shape} time costs: {batch_end-batch_start:.4f}s")
    torch.cuda.synchronize()
    end = time.time()
    total_num_tokens = np.sum(num_tokens)
    throughput = total_num_tokens / (end - start)
    print(f"Throughput: {total_num_tokens} tokens/{end-start} sec = {throughput} tokens/s")


def prefetch_experts_by_predictor(model, input_ids, attention_mask, predictor):
    if predictor == 'random': # for debug
        pattern_matrix = torch.randint(0, 2, (32, 8))
    else:
        ...
    
    for i, layer in enumerate(model.model.layers):
        layer.block_sparse_moe.experts.prefetch(pattern_matrix)


def custom_generate(
    input_ids,
    attention_mask,
    model,
    max_new_tokens=128,
    past_key_values=None,
    temperature=0.9,
    top_p=0.9,
    predictor=None
):
    """
    Generate text from an input using caching and sampling techniques.

    Args:
    input_ids (torch.Tensor): Tensor of token ids to be fed to the model.
    attention_mask (torch.Tensor): Tensor representing the attention mask.
    model (transformers.PreTrainedModel): The model to use for generating text.
    tokenizer (transformers.PreTrainedTokenizer): Tokenizer associated with the model.
    max_new_tokens (int): Maximum number of tokens to generate.
    temperature (float): Sampling temperature for controlling generation randomness.
    top_p (float): Nucleus sampling cutoff probability.

    Returns:
    torch.Tensor: Tensor containing the generated token ids.
    """
    model.eval()  # Put model in evaluation mode
    with torch.no_grad():  # Disable gradient calculation
        # Initialize variables to store outputs and past_key_values
        generated_token_ids = input_ids
        crt_tokens = input_ids
        router_logits = []

        for _ in range(max_new_tokens+1):
            if predictor is not None:
                prefetch_experts_by_predictor(
                    model, generated_token_ids, attention_mask, predictor
                )
            outputs = model(
                input_ids=crt_tokens,
                attention_mask=attention_mask,
                past_key_values=past_key_values,
                output_router_logits=True,
                use_cache=True  # Informs the model to return past key-values
            )

            # Update past_key_values for the next iteration
            past_key_values = outputs.past_key_values

            # Obtain logits
            logits = outputs.logits[:, -1, :] / temperature

            # Apply top-p nucleus sampling
            if top_p is not None:
                filtered_logits = top_p_filtering(logits, top_p=top_p)
            else:
                filtered_logits = logits
            probabilities = torch.nn.functional.softmax(filtered_logits, dim=-1)

            # Sample from the filtered distribution
            next_token_id = torch.multinomial(probabilities, num_samples=1)
            crt_tokens = next_token_id
            generated_token_ids = torch.cat((generated_token_ids, next_token_id), dim=1)

            # Update the attention_mask for new token
            attention_mask = torch.cat([attention_mask, torch.ones((input_ids.size(0), 1), device=attention_mask.device)], dim=-1)
            router_logits.append(outputs.router_logits)

        merged_router_logits = []
        num_layers = len(router_logits[0])
        for i in range(num_layers):
            layer_logits = [logit[i] for logit in router_logits]
            merged_logits = torch.cat(layer_logits, dim=0)
            merged_router_logits.append(merged_logits)
        return generated_token_ids, merged_router_logits


def top_p_filtering(logits, top_p=0.9):
    """
    Filter a distribution of logits using nucleus (top-p) sampling

    Args:
    logits (torch.Tensor): The logits output by the model.
    top_p (float): The cumulative probability cutoff for nucleus sampling.

    Returns:
    torch.Tensor: The filtered logits.
    """
    sorted_logits, sorted_indices = torch.sort(logits, descending=True)
    cumulative_probs = torch.cumsum(torch.nn.functional.softmax(sorted_logits, dim=-1), dim=-1)

    # Remove tokens with cumulative probability above the threshold
    sorted_indices_to_remove = cumulative_probs > top_p
    # Shift the indices to the right to keep the first token above the threshold
    sorted_indices_to_remove[..., 1:] = sorted_indices_to_remove[..., :-1].clone()
    sorted_indices_to_remove[..., 0] = 0

    # Scatter sorted tensors to original indexing
    indices_to_remove = sorted_indices_to_remove.scatter(1, sorted_indices, sorted_indices_to_remove)
    logits[indices_to_remove] = float('-inf')
    return logits


def run_benchmark_with_patterns(model, tokenizer, batch_size, max_new_tokens, device, pattern_matrices):
    def prefetch_experts_by_pattern_matrices(model, pattern_matrix):
        for i, layer in enumerate(model.model.layers):
            layer.block_sparse_moe.experts.prefetch(pattern_matrix)
            break

    def create_attention_mask(token_ids):
        # token_ids 是一个 (num_samples, seq_len) 的 PyTorch 张量
        seq_len = token_ids.size(1)
        
        # 找到每行第一个1出现的位置
        # cumsum 累积和将从第一个1开始生成非零值
        ones_and_zeros = (token_ids == 1).long()  # 将token等于1的位置变为1，其余为0
        cum_sum = torch.cumsum(ones_and_zeros, dim=1)
        
        # 生成 mask：cum_sum 大于0的位置表示这之后（包括该位置）应该是1
        attention_mask = cum_sum > 0

        return attention_mask.to(token_ids.device)

    def custom_generate_with_fixed_data(
        batch,
        model,
        max_new_tokens=128,
        past_key_values=None,
        temperature=0.9,
        top_p=0.9,
    ):
        model.eval()  # Put model in evaluation mode
        get_batch_data = lambda key, batch: torch.stack([batch[i][key] for i in range(len(batch))], dim=0)
        num_layers, num_experts = batch[0]['token_pattern_matrices'].shape[-2:]
        all_pattern_matrices = get_batch_data('token_pattern_matrices', batch) # (num_samples, prompt_len, 32, 8)
        all_token_ids = get_batch_data('token_ids', batch) # (num_samples, prompt_len+decoding_len)
        attention_mask = None
        with torch.no_grad():  # Disable gradient calculation
            # Initialize variables to store outputs and past_key_values
            generated_token_ids = None
            crt_tokens = None

            for token_index in range(max_new_tokens):
                if token_index == 0:
                    # prefilling
                    prompt_len = len(batch[0]['prompt_token_ids'])
                    pattern_matrices = all_pattern_matrices[:, :prompt_len, :, :] # (num_samples, prompt_len, 32, 8)
                    pattern_matrix = pattern_matrices.sum(0).sum(0) # (32, 8)
                    crt_tokens = all_token_ids[:, :prompt_len]
                    generated_token_ids = crt_tokens
                    attention_mask = get_batch_data('prompt_attention_mask', batch).to(crt_tokens.device)
                else:
                    # decoding
                    pattern_matrices = all_pattern_matrices[:, prompt_len+token_index-1, :, :] # (num_samples, 32, 8)
                    pattern_matrix = pattern_matrices.sum(0) # (32, 8)
                    crt_tokens = all_token_ids[:, prompt_len+token_index-1].view(-1, 1)
                    attention_mask = torch.cat([attention_mask, torch.ones((len(batch), 1), device=attention_mask.device)], dim=-1)
                    generated_token_ids = torch.cat((generated_token_ids, crt_tokens), dim=1)
                prefetch_experts_by_pattern_matrices(
                    model, pattern_matrix
                )
                outputs = model(
                    input_ids=crt_tokens,
                    attention_mask=attention_mask,
                    past_key_values=past_key_values,
                    use_cache=True  # Informs the model to return past key-values
                )

                # Update past_key_values for the next iteration
                past_key_values = outputs.past_key_values

                # Obtain logits
                logits = outputs.logits[:, -1, :] / temperature

                # Apply top-p nucleus sampling
                if top_p is not None:
                    filtered_logits = top_p_filtering(logits, top_p=top_p)
                else:
                    filtered_logits = logits
                probabilities = torch.nn.functional.softmax(filtered_logits, dim=-1)

                # Sample from the filtered distribution
                next_token_id = torch.multinomial(probabilities, num_samples=1)

                # Update the attention_mask for new token
                attention_mask2 = torch.cat([attention_mask, torch.ones((len(batch), 1), device=attention_mask.device)], dim=-1)

            return generated_token_ids

    # Initialize a dictionary to hold the activations
    # pattern_matrices = {
    #     0: {
    #         'prompt_text': "this is a prompt",
    #         'prompt_token_ids': [0, 2, 3, 56, 956, ...], # 大于等于 prompt
    #         'token_ids': [0, 2, 3, 56, 956, ...], # pad + prompt + decode
    #         'token_pattern_matrices': # 大小为(seq_len, num_layers, num_experts)的 one-hot 矩阵
    #     },
    #     1: {},
    #     ...
    # }
    num_tokens = []
    for i in range(len(pattern_matrices)):
        pattern_matrices[i]['token_ids'] = pattern_matrices[i]['token_ids'].to(device)
        pattern_matrices[i]['token_pattern_matrices'] = pattern_matrices[i]['token_pattern_matrices'].to(device)
    
    torch.cuda.synchronize()
    start = time.time()
    batch_indices = list(pattern_matrices.keys())
    batch_indices = [batch_indices[i:i + batch_size] for i in range(0, len(batch_indices), batch_size)]
    batches = [[pattern_matrices[i] for i in indices] for indices in batch_indices]

    for batch_idx, batch in enumerate(batches):
        batch_start = time.time()
        generated_token_ids = custom_generate_with_fixed_data(
            batch, model, max_new_tokens=max_new_tokens
        )
        batch_end = time.time()
        num_tokens.append(generated_token_ids.numel())
        print(f"Processing batch {batch_idx} generated_token_ids.shape={generated_token_ids.shape} time costs: {batch_end-batch_start:.4f}s")
    torch.cuda.synchronize()
    end = time.time()
    total_num_tokens = np.sum(num_tokens)
    throughput = total_num_tokens / (end - start)
    print(f"Throughput: {total_num_tokens} tokens/{end-start} sec = {throughput} tokens/s")


def test_custom_generate():
    # Load model directly
    from transformers import AutoTokenizer, AutoModelForCausalLM

    tokenizer = AutoTokenizer.from_pretrained("facebook/opt-125m")
    tokenizer.padding_side = 'left'
    model = AutoModelForCausalLM.from_pretrained("facebook/opt-125m")
    data = tokenizer(
        [
        'tell me a joke',
        'summary: this is a love story, where you and I live happily with the beautiful world',
        ], return_tensors='pt', return_attention_mask=True, padding=True
    )
    generated_token_ids, router_logits = custom_generate(
        data.input_ids.to('cuda'),
        data.attention_mask.to('cuda'),
        model.to('cuda'),
        max_new_tokens=128,
        temperature=0.9,
        top_p=0.9,
    )
    print(tokenizer.batch_decode(generated_token_ids.cpu().numpy().tolist(), skip_special_tokens=True))

    
def init_distributed_mode(args=SimpleNamespace()):
    def find_free_port(start_port: int, end_port: int):
        """
        Find a free port within the specified range.
        """
        for port in range(start_port, end_port):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.bind(("", port))  # Try to bind to the port
                s.close()  # Close the socket if successful
                return port
            except OSError as e:
                # print(f"Port {port} is in use, trying next port.")
                continue
        raise RuntimeError(f"No free ports found in range {start_port}-{end_port}")
    if 'RANK' in os.environ and 'WORLD_SIZE' in os.environ and "LOCAL_RANK" in os.environ:
        args.world_size = int(os.environ['WORLD_SIZE'])
        args.rank = int(os.environ["RANK"])
        args.gpu = int(os.environ['LOCAL_RANK'])
        args.local_rank = args.gpu
        args.dist_url = 'env://'
    else:
        os.environ['MASTER_ADDR'] = "127.0.0.1"
        os.environ['MASTER_PORT'] = str(find_free_port(9000, 10000))
        os.environ['RANK'] = '0'
        os.environ['LOCAL_RANK'] = '0'
        os.environ['WORLD_SIZE'] = '1'
        args.rank = 0
        args.gpu = args.local_rank = 0
        args.world_size = 1
        args.dist_url = 'env://'

    args.distributed = True

    torch.cuda.set_device(args.gpu)
    args.dist_backend = 'nccl'
    print('| distributed init (rank {}): {}, gpu {}'.format(
        args.rank, args.dist_url, args.gpu), flush=True)
    torch.distributed.init_process_group(backend=args.dist_backend, init_method=args.dist_url,
                                         world_size=args.world_size, rank=args.rank)
    torch.distributed.barrier()


if __name__ == '__main__':
    import argparse
    import torch.distributed as dist
    import fairscale.nn.model_parallel.initialize as fs_init
    
    def init_env():
        # define the model
        init_distributed_mode()
        fs_init.initialize_model_parallel(dist.get_world_size())

    init_env()

    # 创建 ArgumentParser 对象
    parser = argparse.ArgumentParser(description='Benchmark on a single GPU')

    # 添加参数
    parser.add_argument('--task', type=int, choices=[0, 1, 2, 3], default='0', help='Task to perform')
    # 0: running original implementation
    # 1: get and save pattern matrices for given bacthes of requests, including prefilling and decoding tokens
    # 2: run custom_generate with prefetched pattern matrices
    # 3: run custom_generate with pattern matrices predictor
    parser.add_argument('--pattern_matrices_path', type=str, default='pattern_matrices.pt', help='Path to pattern matrices')
    parser.add_argument('--ipdb', action='store_true', help='Enable ipdb on error')

    # 解析命令行输入
    args = parser.parse_args()
    main(args)
    # test_custom_generate()

# torchrun --nproc_per_node=1 --master_port=26173  benchmark_single_gpu_switch.py --task 0