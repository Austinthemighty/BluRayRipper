import asyncio

import pytest

from blurayripper.scheduler import EncodeScheduler, encode_weight
from blurayripper.tools.mkvtools import parse_video_height

SAMPLE_UHD = '{"tracks":[{"type":"video","properties":{"pixel_dimensions":"3840x2160"}}]}'
SAMPLE_HD = '{"tracks":[{"type":"video","properties":{"pixel_dimensions":"1920x1080"}}]}'
SAMPLE_NONE = '{"tracks":[{"type":"audio","properties":{}}]}'


def test_video_height_parse():
    assert parse_video_height(SAMPLE_UHD) == 2160
    assert parse_video_height(SAMPLE_HD) == 1080
    assert parse_video_height(SAMPLE_NONE) is None


def test_encode_weight():
    assert encode_weight(2160) == 2
    assert encode_weight(1080) == 1
    assert encode_weight(480) == 1
    assert encode_weight(None) == 1  # unknown -> treat as light


async def test_uhd_and_light_run_together_but_two_uhd_do_not():
    sched = EncodeScheduler(capacity=3)
    a = await sched.acquire(2)  # UHD
    b = await sched.acquire(1)  # 1080p alongside -> fits (2+1=3)
    assert (sched._in_use) == 3

    # A second UHD must wait until something frees up.
    waiter = asyncio.create_task(sched.acquire(2))
    await asyncio.sleep(0.01)
    assert not waiter.done()  # blocked: 3 + 2 > 3

    await sched.release(a)  # free the first UHD (back to 1 in use)
    await sched.release(b)
    acquired = await asyncio.wait_for(waiter, timeout=1)
    assert acquired == 2


async def test_multiple_dvds_run_together():
    sched = EncodeScheduler(capacity=3)
    grants = await asyncio.gather(*(sched.acquire(1) for _ in range(3)))
    assert grants == [1, 1, 1]
    assert sched._in_use == 3


async def test_single_overweight_job_does_not_deadlock():
    sched = EncodeScheduler(capacity=1)
    w = await asyncio.wait_for(sched.acquire(2), timeout=1)  # capped to capacity
    assert w == 1
