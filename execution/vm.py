#!/usr/bin/env python3
import os
from pathlib import Path
import re
import sys

ROOT = Path(os.environ.get("PAAS_EXEC_ROOT", "/srv/ovh-paas-execution"))


def arguments(role):
    if role == "builder":
        ssh, http, memory = 22099, None, 4096
    elif re.fullmatch(r"runtime[0-3]", role):
        slot = int(role[-1])
        ssh, http, memory = 22100 + slot, 22200 + slot, 6144
    else:
        raise ValueError("Unknown execution VM role")
    directory = ROOT / "vm" / role
    network = f"user,id=net0,net=10.201.0.0/24,host=10.201.0.2,dns=10.201.0.3,hostfwd=tcp:127.0.0.1:{ssh}-:22"
    if http:
        network += f",hostfwd=tcp:127.0.0.1:{http}-:80"
    return ["/usr/bin/qemu-system-x86_64", "-name", "paas-" + role, "-machine", "q35,accel=kvm", "-cpu", "host",
            "-smp", "2", "-m", str(memory), "-display", "none", "-nodefaults",
            "-serial", f"file:{directory}/serial.log", "-monitor", f"unix:{directory}/monitor.sock,server=on,wait=off",
            "-drive", f"file={directory}/root.qcow2,if=virtio,format=qcow2,discard=unmap",
            "-drive", f"file={directory}/seed.iso,if=virtio,format=raw,readonly=on",
            "-netdev", network, "-device", "virtio-net-pci,netdev=net0"]


if __name__ == "__main__":
    command = arguments(sys.argv[1])
    os.execv(command[0], command)
