import pytest
import colossalai
import torch
import torch.multiprocessing as mp
import torch.distributed as dist
from colossalai.testing import rerun_if_address_is_in_use
from colossalai.utils.cuda import get_current_device
from colossalai.utils import free_port
from colossalai.utils.model.colo_init_context import ColoInitContext

from functools import partial
from tests.test_tensor.common_utils import set_seed
from tests.components_to_test.registry import non_distributed_component_funcs
from colossalai.nn.parallel import ZeroDDP
from colossalai.zero import ZeroOptimizer
from colossalai.testing import parameterize
from colossalai.gemini.gemini_mgr import GeminiManager
from tests.test_tensor.common_utils import debug_print

from colossalai.gemini.chunk import search_chunk_configuration, ChunkManager


@parameterize('placement_policy', ['cuda', 'cpu', 'auto'])
@parameterize('keep_gathered', [True, False])
def exam_zero_optim_state_dict(placement_policy, keep_gathered):
    set_seed(431)
    get_components_func = non_distributed_component_funcs.get_callable('gpt2')
    model_builder, train_dataloader, test_dataloader, optimizer_class, criterion = get_components_func()

    with ColoInitContext(device=get_current_device()):
        model = model_builder()

    set_seed(451)
    torch_model = model_builder()    # get a different model

    world_size = torch.distributed.get_world_size()
    config_dict = search_chunk_configuration(model, search_range_mb=1, search_interval_byte=100)
    config_dict[world_size]['chunk_size'] = 5000
    config_dict[world_size]['keep_gathered'] = keep_gathered

    if placement_policy != 'cuda':
        init_device = torch.device('cpu')
    else:
        init_device = None
    chunk_manager = ChunkManager(config_dict, init_device=init_device)
    gemini_manager = GeminiManager(placement_policy, chunk_manager)
    model = ZeroDDP(model, gemini_manager, pin_memory=True)

    optimizer = torch.optim.Adam(model.parameters())
    optim = ZeroOptimizer(optimizer, model)    # initialize the link between chunk16 and chunk32

    set_seed(dist.get_rank() * 3 + 128)
    for i, (input_ids, attn_mask) in enumerate(train_dataloader):
        if i > 0:
            break
        optim.zero_grad()
        logits = model(input_ids, attn_mask)
        logits = logits.float()
        loss = criterion(logits, input_ids)
        optim.backward(loss)

    optim_state_dict = optim.state_dict()
    optim.load_state_dict(optim_state_dict)


def run_dist(rank, world_size, port):
    config = {}
    colossalai.launch(config=config, rank=rank, world_size=world_size, host='localhost', port=port, backend='nccl')
    exam_zero_optim_state_dict()


@pytest.mark.dist
@pytest.mark.parametrize('world_size', [1, 4])
@rerun_if_address_is_in_use()
def test_zero_optim(world_size):
    run_func = partial(run_dist, world_size=world_size, port=free_port())
    mp.spawn(run_func, nprocs=world_size)


if __name__ == '__main__':
    test_zero_optim(1)