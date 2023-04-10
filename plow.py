#!/usr/bin/env python3
"""
The Plow.

An efficient Chia plot mover.

Author: Luke Macken <phorex@protonmail.com>
SPDX-License-Identifier: GPL-3.0-or-later
"""
import sys
import glob
import shutil
import random
import urllib.request
import asyncio
import aionotify
from pathlib import Path
from datetime import datetime
from collections import defaultdict

# Local plot sources
# For wildcards:
#   SOURCES = glob.glob('/mnt/*')
SOURCES = []

# Rsync destinations
# Examples: ["/mnt/HDD1", "192.168.1.10::hdd1"]
DESTS = []

# Shuffle plot destinations. Useful when using many plotters to decrease the odds
# of them copying to the same drive simultaneously.
SHUFFLE = True 

# Rsync bandwidth limiting
BWLIMIT = None

# Optionally set the I/O scheduling class and priority
IONICE = None  # "-c 3" for "idle"

# Only send 1 plot at a time, regardless of source/dest. 
ONE_AT_A_TIME = False

# Each plot source can have a lock, so we don't send more than one file from
# that origin at any given time.
ONE_PER_DRIVE = False

# Short & long sleep durations upon various error conditions
SLEEP_FOR = 60 * 3
SLEEP_FOR_LONG = 60 * 20

RSYNC_CMD = "rsync"

if SHUFFLE:
    random.shuffle(DESTS)


# Rsync parameters. For FAT/NTFS you may need to remove --preallocate
if BWLIMIT:
    RSYNC_FLAGS = f"--remove-source-files --preallocate --whole-file --bwlimit={BWLIMIT}"
else:
    RSYNC_FLAGS = "--remove-source-files --preallocate --whole-file"

if IONICE:
    RSYNC_CMD = f"ionice {IONICE} {RSYNC_CMD}"

LOCK = asyncio.Lock()  # Global ONE_AT_A_TIME lock
SRC_LOCKS = defaultdict(asyncio.Lock)  # ONE_PER_DRIVE locks


async def plotfinder(paths, plot_queue, loop):
    for path in paths:
        for plot in Path(path).glob("**/*.plot"):
            await plot_queue.put(plot)
    await plotwatcher(paths, plot_queue, loop)


async def plotwatcher(paths, plot_queue, loop):
    watcher = aionotify.Watcher()
    for path in paths:
        if not Path(path).exists():
            print(f'! Path does not exist: {path}')
            continue
        print('watching', path)
        watcher.watch(
            alias=path,
            path=path,
            flags=aionotify.Flags.MOVED_TO,
        )
    await watcher.setup(loop)
    while True:
        event = await watcher.get_event()
        print(event)
        if event.name.endswith(".plot"):
            plot_path = Path(event.alias) / event.name
            await plot_queue.put(plot_path)


async def plow(dest, plot_queue, loop):
    print(f"🧑‍🌾 plowing to {dest}")
    while True:
        try:
            plot = await plot_queue.get()
            cmd = f"{RSYNC_CMD} {RSYNC_FLAGS} {plot} {dest}"

            # For local copies, we can check if there is enough space.
            dest_path = Path(dest)
            if dest_path.exists():
                # Make sure it's actually a mount, and not our root filesystem.
                if not dest_path.is_mount():
                    print(f"Farm destination {dest_path} is not mounted. Trying again later.")
                    await plot_queue.put(plot)
                    await asyncio.sleep(SLEEP_FOR)
                    continue

                plot_size = plot.stat().st_size
                dest_free = shutil.disk_usage(dest).free
                if dest_free < plot_size:
                    print(f"Farm {dest} is full")
                    await plot_queue.put(plot)
                    # Just quit the worker entirely for this destination.
                    break

            # One at a time, system-wide lock
            if ONE_AT_A_TIME:
                await LOCK.acquire()

            # Only send one plot from each SSD at a time
            if ONE_PER_DRIVE:
                await SRC_LOCKS[plot.parent].acquire()

            try:
                print(f"🚜 {plot} ➡️  {dest}")

                # Send a quick test copy to make sure we can write, or fail early.
                test_cmd = f"rsync /etc/hostname {dest}"
                proc = await asyncio.create_subprocess_shell(
                    test_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode != 0:
                    print(f"⁉️  {test_cmd!r} exited with {proc.returncode}")
                    await plot_queue.put(plot)
                    break

                # Now rsync the real plot
                proc = await asyncio.create_subprocess_shell(
                    cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                start = datetime.now()
                stdout, stderr = await proc.communicate()
                finish = datetime.now()
            finally:
                if ONE_PER_DRIVE:
                    SRC_LOCKS[plot.parent].release()
                if ONE_AT_A_TIME:
                    LOCK.release()

            if proc.returncode == 0:
                print(f"🏁 {cmd} ({finish - start})")
            elif proc.returncode == 10:  # Error in socket I/O
                # Retry later.
                print(f"⁉️ {cmd!r} exited with {proc.returncode} (error in socket I/O)")
                await plot_queue.put(plot)
                await asyncio.sleep(SLEEP_FOR_LONG)
            elif proc.returncode in (11, 23):  # Error in file I/O
                # Most likely a full drive.
                print(f"⁉️ {cmd!r} exited with {proc.returncode} (error in file I/O)")
                await plot_queue.put(plot)
                print(f"{dest} plow exiting")
                break
            else:
                print(f"⁉️ {cmd!r} exited with {proc.returncode}")
                await asyncio.sleep(SLEEP_FOR)
                await plot_queue.put(plot)
                print(f"{dest} plow exiting")
                break
            if stdout:
                output = stdout.decode().strip()
                if output:
                    print(f"{stdout.decode()}")
            if stderr:
                print(f"⁉️ {stderr.decode()}")
        except Exception as e:
            print(f"! {e}")


async def main(paths, loop):
    plot_queue = asyncio.Queue()
    futures = []

    # Add plots to queue
    futures.append(plotfinder(paths, plot_queue, loop))

    # Fire up a worker for each plow
    for dest in DESTS:
        futures.append(plow(dest, plot_queue, loop))

    print('🌱 Plow running...')
    await asyncio.gather(*futures)


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main(SOURCES, loop))
    except KeyboardInterrupt:
        pass
