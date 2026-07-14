#!/usr/bin/env python3
import argparse
import time
import sys
import os

from concurrent.futures import ThreadPoolExecutor, wait

import file_p2p

# Check for torch
try:
    import torch
except ImportError:
    print("Error: PyTorch is required. Please install it (pip install torch).")
    sys.exit(1)

import torch_npu


def run_performance_test(devices, iodepth, block_size):
    """
    Runs the NVMe to GPU read performance test using Async I/O.
    """

    # Check CUDA availability
    #if not torch.cuda.is_available():
    #    print("Error: CUDA is not available. This test requires a GPU.")
    #    sys.exit(1)

    dev_nr = len(devices)

    npu_dev_ids = ["npu:0", "npu:1"]
    print(f"Allocating {iodepth} GPU buffers on {npu_dev_ids}...")

    gpu_buffers = []
    try:
        # Allocate gpu buffer in bs granularity one-by-one
        for i in range(iodepth * dev_nr):
            npu_dev_idx = i // iodepth
            npu_dev_idx = npu_dev_idx % len(npu_dev_ids)
            buf = torch.empty(block_size, dtype=torch.uint8, device=npu_dev_ids[npu_dev_idx])
            gpu_buffers.append(buf)
    except Exception as e:
        print(f"Failed to allocate GPU memory: {e}")
        raise
        sys.exit(1)

    total_transfer_size = iodepth * block_size * dev_nr
    print(f"Buffers allocated. Total Size: {total_transfer_size / (1024**2):.2f} MB")
    print(f"Block Size: {block_size / 1024:.0f} KB | IODepth: {iodepth}")
    print("-" * 60)

    def device_worker(p2p_fd, start_offset, dev_idx, dev_path):
        dev_path = dev_path.strip()
        #print(f"Testing Device: {dev_path}")

        # 1. Issue Async Reads
        # We assume gds.read is async and returns immediately with a handle
        for i in range(iodepth):
            offset = start_offset + i * block_size
            # Get address of the specific buffer for this IO
            buf_idx = dev_idx * iodepth + i
            dest_addr = gpu_buffers[buf_idx].data_ptr()

            # Issue read
            file_p2p.read_file(p2p_fd, dev_path, dev_path, offset, dest_addr, block_size, 0, 0)

    default_offset = 128 << 20
    default_gap = 4 << 30
    start_offset_list = []
    read_devices = {}
    for d in devices:
        if d not in read_devices:
            start_offset = default_offset
            read_devices[d] = 1
        else:
            start_offset = default_offset + read_devices[d] * default_gap
            read_devices[d] += 1
        start_offset_list.append(start_offset)

    p2p_fd = file_p2p.new_p2p_fd()
    if p2p_fd < 0:
        print("new p2p fd failed")
        sys.exit(1)

    # Start timing
    start_time = time.time()

    total_bytes = 0
    while True:
        with ThreadPoolExecutor(max_workers=dev_nr) as executor:
            futures = []
            for dev_idx, dev_path in enumerate(devices):
                start_offset = start_offset_list[dev_idx]
                futures.append(executor.submit(device_worker, p2p_fd, start_offset, dev_idx, dev_path))

            # Wait for all threads to finish
            wait(futures)

        # 2. Wait for Completions
        err = file_p2p.drain_read(p2p_fd)
        if err:
            break

        total_bytes = total_bytes + total_transfer_size

        cur_time = time.time()
        if cur_time > start_time + 10:
            break

    end_time = time.time()

    # Calculate metrics
    duration = end_time - start_time
    throughput_bps = total_bytes / duration
    throughput_gbps = throughput_bps / (1024**3)

    print(f"  -> Completed {iodepth} reads of {block_size} bytes from {dev_nr} devices.")
    print(f"  -> Time: {duration:.4f} seconds")
    print(f"  -> Throughput: {throughput_gbps:.2f} GB/s")
    print("-" * 60)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test NVMe to GPU HBM read performance via GDS.")

    parser.add_argument(
        "--devices",
        type=str,
        default="/dev/nvme0n1",
        help="Comma-separated list of NVMe devices (default: /dev/nvme0n1)"
    )

    parser.add_argument(
        "--iodepth",
        type=int,
        default=128,
        help="Number of concurrent IO requests (default: 32)"
    )

    parser.add_argument(
        "--block-size",
        type=str,
        default="128KB",
        help="Block size for each read (default: 128KB). Supports KB/MB suffixes."
    )

    args = parser.parse_args()

    # Parse Block Size
    bs_str = args.block_size.upper()
    if bs_str.endswith("KB"):
        bs = int(float(bs_str.replace("KB", "")) * 1024)
    elif bs_str.endswith("MB"):
        bs = int(float(bs_str.replace("MB", "")) * 1024 * 1024)
    else:
        bs = int(bs_str)

    # Parse Devices List
    device_list = [d.strip() for d in args.devices.split(",") if d.strip()]

    run_performance_test(device_list, args.iodepth, bs)


