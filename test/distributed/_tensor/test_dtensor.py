# Copyright (c) Meta Platforms, Inc. and affiliates
# Owner(s): ["oncall: distributed"]

import torch
import torch.nn.functional as F
from torch.distributed.tensor.parallel import (
    PairwiseParallel,
    parallelize_module,
)
from torch.distributed._tensor import DeviceMesh, distribute_tensor, DTensor
from torch.distributed._tensor.placement_types import _Partial, Replicate, Shard

from torch.testing._internal.common_utils import run_tests
from torch.testing._internal.distributed._tensor.common_dtensor import (
    DTensorTestBase,
    with_comms,
)

class DummyMLP(torch.nn.Module):
    def __init__(self, device):
        super(DummyMLP, self).__init__()
        self.net1 = torch.nn.Linear(5, 1024, device=device)
        self.relu = torch.nn.ReLU()
        self.net2 = torch.nn.Linear(1024, 4, device=device)

    def forward(self, x):
        return self.net2(F.relu(self.net1(x)))

    def reset_parameters(self, *args, **kwargs):
        with torch.no_grad():
            self.net1.weight.fill_(0.5)
            self.net2.weight.fill_(1)
            self.net1.bias.fill_(1.5)
            self.net2.bias.fill_(1.2)

class DTensorTest(DTensorTestBase):
    @with_comms
    def test_dtensor_constructor(self):
        device_mesh = DeviceMesh(self.device_type, list(range(self.world_size)))
        shard_spec = [Shard(0)]
        local_tensor = torch.randn(3, 3, requires_grad=True)
        dist_tensor_shape = torch.Size([self.world_size * 3, 3])
        dist_tensor = DTensor(
            local_tensor,
            device_mesh,
            shard_spec,
            size=dist_tensor_shape,
            requires_grad=True,
        )
        self.assertEqual(dist_tensor.size(), torch.Size((self.world_size * 3, 3)))

        with self.assertWarnsRegex(UserWarning, "To construct"):
            DTensor(local_tensor, device_mesh, shard_spec, size=dist_tensor_shape)

        local_tensor = torch.randn(3, 3, requires_grad=False)
        with self.assertWarnsRegex(UserWarning, "To construct"):
            dist_tensor = DTensor(
                local_tensor,
                device_mesh,
                shard_spec,
                size=dist_tensor_shape,
                requires_grad=True,
            )

    @with_comms
    def test_meta_dtensor(self):
        device_mesh = self.build_device_mesh()
        dist_specs = [[Shard(0)], [Replicate()]]
        meta_tensor = torch.randn(1024, 2048, device="meta")
        for dist_spec in dist_specs:
            # Test distribute_tensor on meta tensor
            meta_dtensor = distribute_tensor(meta_tensor, device_mesh, dist_spec)
            self.assertTrue(meta_dtensor.is_meta)
            meta_dtensor = torch.empty_like(meta_dtensor, device=self.device_type)
            torch.nn.init.constant_(meta_dtensor, 1.2)
            value_tensor = torch.empty_like(meta_dtensor.to_local()).fill_(1.2)
            self.assertFalse(meta_dtensor.is_meta)
            self.assertEqual(meta_dtensor.device.type, self.device_type)
            self.assertEqual(meta_dtensor.to_local(), value_tensor)
            # Test from_local on meta tensor
            meta_dtensor = DTensor.from_local(meta_tensor, device_mesh, dist_spec)
            meta_dtensor = torch.empty_like(meta_dtensor, device=self.device_type)
            torch.nn.init.constant_(meta_dtensor, 1.5)
            self.assertEqual(meta_dtensor.device.type, self.device_type)
            value_tensor = torch.empty_like(meta_dtensor.to_local()).fill_(1.5)
            self.assertEqual(meta_dtensor.to_local(), value_tensor)

    @with_comms
    def test_modules_w_meta_dtensor(self):
        model = DummyMLP("meta")
        device_mesh = self.build_device_mesh()
        model_tp = parallelize_module(model, device_mesh, PairwiseParallel())
        model_tp.to_empty(device=self.device_type)
        model_tp.reset_parameters()
        optim = torch.optim.SGD(model_tp.parameters(), lr=0.1)
        model_regular = DummyMLP(self.device_type)
        model_regular_tp = parallelize_module(model_regular, device_mesh, PairwiseParallel())
        optim_regular = torch.optim.SGD(model_regular_tp.parameters(), lr=0.1)
        model_regular_tp.reset_parameters()
        torch.manual_seed(0)
        inp = torch.randn(20, 5, device=self.device_type)

        output = model_tp(inp)
        output_regular = model_regular_tp(inp)
        self.assertEqual(output, output_regular)

        output.sum().backward()
        output_regular.sum().backward()

        optim.step()
        optim_regular.step()

        torch.manual_seed(1)
        inp = torch.randn(20, 5, device=self.device_type)
        self.assertEqual(model_tp(inp), model_regular_tp(inp))

    @with_comms
    def test_dtensor_stride(self):
        device_mesh = DeviceMesh(self.device_type, list(range(self.world_size)))
        shard0_spec = [Shard(0)]
        local_tensor = torch.randn(4, 8)
        global_shape = torch.Size([self.world_size * 4, 8])
        dist_tensor = DTensor(local_tensor, device_mesh, shard0_spec, size=global_shape)
        # won't affect stride
        self.assertEqual(dist_tensor.stride(), (8, 1))

        shard1_spec = [Shard(1)]
        local_tensor = torch.randn(8, 4)
        global_shape = torch.Size([8, self.world_size * 4])
        dist_tensor = DTensor(local_tensor, device_mesh, shard1_spec, size=global_shape)
        # will affect stride after DT initialized
        self.assertEqual(dist_tensor.stride(), (4 * self.world_size, 1))

        # if initialized from a transposed mat
        local_tensor = torch.randn(8, 4, 8)
        local_tensor_t = local_tensor.permute(1, 2, 0)
        global_shape = torch.Size([4, self.world_size * 8, 8])
        self.assertEqual(local_tensor_t.stride(), (8, 1, 32))
        dist_tensor = DTensor(
            local_tensor_t, device_mesh, shard1_spec, size=global_shape
        )
        global_stride = (8 * self.world_size, 1, 32 * self.world_size)
        self.assertEqual(dist_tensor.stride(), global_stride)

    @with_comms
    def test_from_local(self):
        device_mesh = DeviceMesh(self.device_type, list(range(self.world_size)))
        shard_spec = [Shard(0)]
        local_tensor = torch.randn(3, 3)
        sharded_tensor = DTensor.from_local(local_tensor, device_mesh, shard_spec)
        self.assertEqual(sharded_tensor.size(), torch.Size([self.world_size * 3, 3]))

        replica_spec = [Replicate()]
        ddp_tensor = DTensor.from_local(local_tensor, device_mesh, replica_spec)
        self.assertEqual(ddp_tensor.size(), local_tensor.size())

        partial_spec = [_Partial()]
        partial_tensor = DTensor.from_local(local_tensor, device_mesh, partial_spec)
        self.assertEqual(partial_tensor.size(), local_tensor.size())

        # test dist tensor works with torch.Tensor during backwards
        local_tensor_with_grad = torch.randn(3, 3, requires_grad=True)
        # do some operations on local tensor
        local_tensor_temp = local_tensor_with_grad * 3
        # create the dist tensor with non leaf local tensor, dist tensor created
        # should also be non leaf node
        dist_tensor = DTensor.from_local(local_tensor_temp, device_mesh, shard_spec)
        self.assertFalse(dist_tensor.is_leaf)
        # do some random operations on dist tensor
        output = dist_tensor * 3
        self.assertIsInstance(output, DTensor)
        # trigger .backward() on dist tensor directly
        local_grad = torch.ones(3, 3)
        grad_output = DTensor.from_local(local_grad, device_mesh, shard_spec)
        # run backward directly on dist tensor
        output.backward(grad_output)
        # check it gradients flow back to original torch.Tensor
        self.assertIsNotNone(local_tensor_with_grad.grad)
        expected_grad = torch.ones(3, 3) * 9
        self.assertEqual(local_tensor_with_grad.grad, expected_grad)

    @with_comms
    def test_to_local(self):
        device_mesh = DeviceMesh(self.device_type, list(range(self.world_size)))
        shard_spec = [Shard(0)]
        dist_tensor_shape = torch.Size([self.world_size * 3, 3])
        local_tensor_with_grad = torch.randn(
            3, 3, device=self.device_type, requires_grad=True
        )

        sharded_tensor = DTensor(
            local_tensor_with_grad,
            device_mesh,
            shard_spec,
            size=dist_tensor_shape,
            requires_grad=True,
        )
        self.assertEqual(sharded_tensor.size(), dist_tensor_shape)
        self.assertEqual(sharded_tensor.to_local(), local_tensor_with_grad)

        # test dist tensor works with torch.Tensor during backwards
        # dist tensor created is a leaf node, do some operation on dist tensor
        temp_st = sharded_tensor * 3

        # do some operation on local tensor of the dist tensor
        new_tensor_with_grad = torch.randn(
            3, 3, device=self.device_type, requires_grad=True
        )
        res = temp_st.to_local() + new_tensor_with_grad
        # call backward directly on torch.Tensor, and see if it works by
        # propagating through dist tensor
        res.sum().backward()
        self.assertIsNotNone(sharded_tensor.grad)

        self.assertEqual(sharded_tensor.grad.to_local(), torch.ones(3, 3) * 3)

    @with_comms
    def test_from_local_then_to_local(self):
        # this test ensure end to end from torch.Tensor -> dist tensor -> torch.Tensor works
        device_mesh = DeviceMesh(self.device_type, list(range(self.world_size)))
        shard_spec = [Shard(0)]

        # step 1. construct from construct local tensor
        local_tensor_with_grad = torch.randn(
            3, 3, device=self.device_type, requires_grad=True
        )
        # do some operations on local tensor
        local_tensor_temp = local_tensor_with_grad + 8
        # step 2. create the dist tensor with non leaf local tensor, dist tensor
        # created should also be non leaf node
        dist_tensor = DTensor.from_local(local_tensor_temp, device_mesh, shard_spec)
        self.assertFalse(dist_tensor.is_leaf)
        # do some random operations on dist tensor
        output = dist_tensor * 6
        self.assertIsInstance(output, DTensor)

        # step 3. do some operation on local tensor of the dist tensor
        new_tensor_with_grad = torch.randn(
            3, 3, device=self.device_type, requires_grad=True
        )
        res = output.to_local() + new_tensor_with_grad
        # call backward directly on torch.Tensor, and see if it works by
        # propagating all the way back to the original torch.Tensor
        res.sum().backward()
        self.assertIsNotNone(local_tensor_with_grad.grad)

        expected_grad = torch.ones(3, 3) * 6
        self.assertEqual(local_tensor_with_grad.grad, expected_grad)

    @with_comms
    def test_dtensor_spec_read_only_after_set(self):
        device_mesh = DeviceMesh(self.device_type, list(range(self.world_size)))
        shard_spec = [Shard(0)]
        local_tensor = torch.randn(3, 3)
        sharded_tensor = DTensor.from_local(local_tensor, device_mesh, shard_spec)

        # modify shard_spec, and dist_tensor's spec should not be changed
        shard_spec[0] = Replicate()
        self.assertTrue(sharded_tensor.placements is not shard_spec)
        self.assertNotEqual(sharded_tensor.placements, shard_spec)

    @with_comms
    def test_dtensor_spec_hash(self):
        device_mesh = DeviceMesh(self.device_type, list(range(self.world_size)))
        shard_spec = [Shard(0)]
        local_tensor = torch.randn(3, 3)
        local_tensor2 = torch.randn(3, 3)
        sharded_tensor = DTensor.from_local(local_tensor, device_mesh, shard_spec)
        sharded_tensor2 = DTensor.from_local(local_tensor2, device_mesh, shard_spec)
        # note that DTensorSpec without real tensor data, so the hash would be the same
        # as long as the mesh, placements and tensor properties are the same
        self.assertEqual(hash(sharded_tensor._spec), hash(sharded_tensor2._spec))

        # change the placements would change the hash
        local_tensor3 = torch.ones(3, 3)
        replica_spec = [Replicate()]
        replica_tensor = DTensor.from_local(
            local_tensor3, device_mesh, replica_spec, run_check=False
        )
        self.assertNotEqual(hash(sharded_tensor._spec), hash(replica_tensor._spec))

    @with_comms
    def test_dtensor_properties(self):
        device_mesh = DeviceMesh(self.device_type, list(range(self.world_size)))
        shard_spec = [Shard(0)]
        local_tensor = torch.randn(3, 3)
        sharded_tensor = DTensor.from_local(local_tensor, device_mesh, shard_spec)
        self.assertEqual(sharded_tensor.device.type, self.device_type)


class DTensorMeshTest(DTensorTestBase):
    @property
    def world_size(self):
        return 8

    @with_comms
    def test_dtensor_device_mesh_device_conversion(self):
        # construct a cuda device mesh
        mesh = DeviceMesh(self.device_type, torch.arange(self.world_size))

        # construct from a cpu local tensor with cuda device mesh
        # should automatically convert the dist tensor to cuda
        shard_spec = [Shard(0)]
        local_tensor = torch.randn(3, 3)
        dist_tensor = DTensor.from_local(local_tensor, mesh, shard_spec)
        self.assertEqual(dist_tensor.device.type, self.device_type)
        self.assertEqual(dist_tensor.to_local().device.type, self.device_type)

    @with_comms
    def test_dtensor_api_device_mesh_context_manager(self):
        with DeviceMesh(self.device_type, list(range(self.world_size))) as mesh:
            shard_spec = [Shard(0)]
            local_tensor = torch.randn(3, 3)
            sharded_tensor = DTensor.from_local(
                local_tensor, device_mesh=mesh, placements=shard_spec
            )

        with DeviceMesh(self.device_type, list(range(self.world_size))):
            shard_spec = [Shard(0)]
            local_tensor = torch.randn(3, 3)
            sharded_tensor = DTensor.from_local(local_tensor, placements=shard_spec)
            replica_spec = [Replicate()]
            replica_tensor = sharded_tensor.redistribute(placements=replica_spec)
            self.assertEqual(
                replica_tensor.size(), torch.Size([3 * self.world_size, 3])
            )

    @with_comms
    def test_dtensor_2d_mesh(self):
        mesh_tensor = torch.arange(self.world_size).reshape(2, 4)
        # construct a cuda device mesh
        mesh = DeviceMesh(self.device_type, mesh_tensor)

        # construct a dist tensor on 2d device mesh and test if works
        shard_spec = [Shard(0), Shard(1)]
        local_tensor = torch.randn(3, 3)
        dist_tensor = DTensor.from_local(local_tensor, mesh, shard_spec)
        self.assertEqual(
            dist_tensor.size(), torch.Size([3 * mesh.size(0), 3 * mesh.size(1)])
        )
        self.assertEqual(dist_tensor.device.type, self.device_type)
        self.assertEqual(dist_tensor.to_local().device.type, self.device_type)

        # if shard on the same tensor dimension
        # we should correctly construct the global tensor size
        shard_same_dim_spec = [Shard(0), Shard(0)]
        local_tensor = torch.randn(3, 3)
        dist_tensor = DTensor.from_local(local_tensor, mesh, shard_same_dim_spec)
        self.assertEqual(dist_tensor.size(), torch.Size([3 * self.world_size, 3]))

    @with_comms
    def test_device_mesh_nd(self):
        # construct a cuda device mesh
        mesh_tensor = torch.arange(self.world_size).reshape(2, 2, 2)
        mesh = DeviceMesh(self.device_type, mesh_tensor)
        # construct a dist tensor on 3d device mesh and test if works
        shard_spec = [Shard(0), Shard(1), Shard(2)]
        local_tensor = torch.randn(3, 3, 3)
        dist_tensor = DTensor.from_local(local_tensor, mesh, shard_spec)
        self.assertEqual(dist_tensor.size(), torch.Size([6, 6, 6]))
        self.assertEqual(dist_tensor.device.type, self.device_type)
        self.assertEqual(dist_tensor.to_local().device.type, self.device_type)

        # construct a dist tensor on 3d device mesh with some shards on same dim
        shard_spec = [Shard(0), Shard(0), Shard(2)]
        local_tensor = torch.randn(3, 3, 3)
        dist_tensor = DTensor.from_local(local_tensor, mesh, shard_spec)
        self.assertEqual(dist_tensor.size(), torch.Size([12, 3, 6]))
        self.assertEqual(dist_tensor.device.type, self.device_type)
        self.assertEqual(dist_tensor.to_local().device.type, self.device_type)

    @with_comms
    def test_dtensor_spec_local_shard_offset(self):
        device_mesh = DeviceMesh(
            self.device_type, torch.arange(self.world_size).reshape(2, 4)
        )
        tensor_shape = (3 * self.world_size, 3 * self.world_size)
        # sharding specs and its corresponding local shard offsets
        shard_spec_and_offsets = [
            (
                [Shard(0), Replicate()],
                (3 * (self.world_size // 2) * (self.rank // 4), 0),
            ),
            (
                [Shard(1), Replicate()],
                (0, 3 * (self.world_size // 2) * (self.rank // 4)),
            ),
            (
                [Replicate(), Shard(0)],
                (3 * (self.world_size // 4) * (self.rank % 4), 0),
            ),
            (
                [Replicate(), Shard(1)],
                (0, 3 * (self.world_size // 4) * (self.rank % 4)),
            ),
        ]

        # loop through all sharding specs and check local shard offsets
        logical_tensor = torch.randn(tensor_shape)
        for shard_spec, expected_shard_offsets in shard_spec_and_offsets:
            dtensor = distribute_tensor(logical_tensor, device_mesh, shard_spec)
            self.assertEqual(expected_shard_offsets, dtensor._spec.local_offsets)


if __name__ == "__main__":
    run_tests()
