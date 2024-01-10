from __future__ import annotations

import argparse
import contextlib
import enum
import getpass
import json
import os
import re
import subprocess
import sys
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.resources import as_file, files
from io import BytesIO, StringIO
from pathlib import Path
from typing import cast

_username = getpass.getuser()
HOME = os.getenv("HOME", os.path.join("/home", _username))
XDG_DATA_HOME = os.getenv("XDG_DATA_HOME", os.path.join(HOME, ".local", "share"))


# Developer note: Want to add support to another runner?
# That's excellent! You need to modify three things:
# 1) You need to add a new runner to the ``SupportedRunner`` enum below. 
# 2) You need to add a branch to ``write_instance_string``. This writes the entries to stdin of the
#    spawned runner process ala ``dmenu``.
# 3) Finally, you need to add a branch to ``get_runner_args`` which will return a list of CLI 
#    arguments to run the actual runner with. You usually want to enable icons and case-insensitive
#    matching there. 
#
# Then, just run ``prism-rofi --runner myrunner``.
#
# Funnily enough, despite using the real dmenu protocol, this doesn't (yet) support the real
# dmenu because I use Wayland and dmenu doesn't work on Wayland. (I also use an NVIDIA GPU. Beware.)


class SupportedRunner(enum.Enum):
    ROFI = "rofi"
    WOFI = "wofi"
    FUZZEL = "fuzzel"


@dataclass
class Instance:
    # prism name
    name: str
    # prism group, set to "ungrouped" if not in a group.
    group: str
    # net.minecraft component version, unset if not found (?)
    game_version: str | None = None
    # loader type found.
    loader: str | None = None
    # the overridden instance icon path.
    icon_path: Path | None = None
    # the modpack version, if any.
    modpack_version: str | None = None

    @property
    @contextmanager
    def real_icon_path(self) -> Generator[Path, None, None]:
        """
        Gets the real icon path for this instance.
        """

        if self.icon_path:
            return (yield self.icon_path)

        name: str
        match self.loader:
            case "forge":
                name = "forge.png"

            case "fabric":
                name = "fabric.png"

            case "quilt":
                name = "quilt.png"

            case "neoforge":
                name = "neoforge.png"

            case _:
                name = "grass.png"

        with as_file(files("prism_rofi.icons").joinpath(name)) as f:
            yield f

    def __str__(self) -> str:
        buf = StringIO()
        buf.write(self.name)
        buf.write(" (")
        buf.write(self.group)

        if self.modpack_version:
            buf.write(", ")
            buf.write(self.modpack_version)

        if self.game_version:
            buf.write(", Minecraft ")
            buf.write(self.game_version)

        buf.write(")")
        return buf.getvalue()
    

def write_instance_string(
    runner: SupportedRunner, 
    buffer: BytesIO,
    instance: Instance,
    icon_path: Path,
):
    """
    Writes a single instance string out to the provided buffer.
    """

    match runner.value:
        case "rofi" | "fuzzel":
            buffer.write(str(instance).encode())
            buffer.write(b"\x00icon\x1f")

            # Note to future readers: the advice online says use ``file://``, but that doesn't
            # work!
            buffer.write(str(icon_path.absolute()).encode())
            buffer.write(b"\n")

        case "wofi":
            buffer.write(b"img:")
            buffer.write(str(icon_path.absolute()).encode())
            buffer.write(b":text:")
            buffer.write(str(instance).encode())
            buffer.write(b"\n")

def get_runner_args(
    runner: SupportedRunner,
    exe_path: str,
) -> list[str]:
    """
    Gets the arguments to use for the specified runner.
    """

    match runner.value:
        case "rofi":
            return [
                exe_path,
                "-dmenu",
                "-format",
                "i",
                "-p",
                "instance",
                "-i",
                "-show-icons",
            ]
        
        case "wofi":
            return [
                exe_path, 
                "--dmenu",
                "--allow-images",
                "--insensitive"
            ]
        
        case "fuzzel":
            return [
                exe_path,
                "--dmenu"
            ]
    
def get_prism_subdir(
    base_dir: Path, 
    config_text: str, 
    key: str,
    default_name: str,
) -> Path:
    """
    Gets the appropriate sub-directory from the Prism Launcher configuration.

    :param base_dir: The base Prism Launcher directory (e.g. ``~/.local/share/PrismLauncher``).
    :param config_text: The contents of the Prism config file.
    :param key: The key to extract from the config text.
    :param default_name: The default name to use if unset, e.g. ``instances``.
    """

    rxp = fr"{key}=(.*)$"
    matched = re.search(rxp, config_text, re.MULTILINE)
    dir_name = Path(default_name if matched is None else matched.group(1))
    if not dir_name.is_absolute():
        dir_name = base_dir / dir_name
    
    return dir_name
    

def main():
    """
    Main entrypoint.
    """

    parser = argparse.ArgumentParser(
        description="Launcher helper script for Prism Launcher instances"
    )

    parser.add_argument(
        "-c",
        "--config-dir",
        help="Path to Prism config dir, uses XDG_DATA_HOME if unset",
        default=os.path.join(XDG_DATA_HOME, "PrismLauncher"),
    )
    parser.add_argument(
        "--runner", 
        help="The type of runner to use", 
        type=SupportedRunner, 
        default=SupportedRunner.ROFI
    )

    parser.add_argument("-e", "--exe", help="Runner executable to launch", default=None)
    parser.add_argument(
        "-p", "--prism", help="Prism Launcher executable to launch", default="prismlauncher"
    )

    args = parser.parse_args()
    config_dir = Path(args.config_dir)

    runner: SupportedRunner = cast(SupportedRunner, args.runner)

    runner_path: str = cast(str, args.exe)
    if args.exe is None:
        runner_path = args.runner.value

    try:
        config_path = config_dir / "prismlauncher.cfg"
        config_data = config_path.read_text()
    except FileNotFoundError:
        print("invalid config dir", file=sys.stderr)
        sys.exit(1)
        

    instance_dir = get_prism_subdir(config_dir, config_data, "InstanceDir", "instances")
    icon_dir = get_prism_subdir(config_dir, config_data, "IconDir", "icons")

    groups = json.loads((instance_dir / "instgroups.json").read_text())
    reverse_groups: dict[str, str] = {}

    if groups["formatVersion"] == "1":
        for gname, body in groups["groups"].items():
            for insn in body["instances"]:
                reverse_groups[insn] = gname

    instances: list[Instance] = []

    for path in instance_dir.iterdir():
        # instances are always directories, and represented with an ``instance.cfg``.
        if not path.is_dir():
            continue

        if not (path / "instance.cfg").exists():
            continue

        instance = Instance(name=path.name, group=reverse_groups.get(path.name, "Ungrouped"))
        instances.append(instance)

        # try and figure out the icon path, if possible.
        instance_cfg_txt = (path / "instance.cfg").read_text()
        icon_match = re.search(r"iconKey=(.*)$", instance_cfg_txt, re.MULTILINE)
        if icon_match is not None:
            icon_name = icon_match.group(1)

            if icon_name != "default":
                instance.icon_path = (icon_dir / icon_name).with_suffix(".png")

        version_match = re.search(r"ManagedPackVersionName=(.*)$", instance_cfg_txt, re.MULTILINE)
        if version_match is not None:
            instance.modpack_version = version_match.group(1)

        # easy way of getting various metadata is to just check the components screen.
        info = json.loads((path / "mmc-pack.json").read_text())

        if info["formatVersion"] != 1:  # why is this an int, but instGroups is a str?
            continue

        for component in info["components"]:
            match component["uid"]:
                case "net.minecraft":
                    instance.game_version = component["version"]

                case "net.fabricmc.fabric-loader":
                    instance.loader = "fabric"

                case "org.quiltmc.quilt-loader":
                    instance.loader = "quilt"

                case "net.minecraftforge":
                    instance.loader = "forge"

                case "net.neoforged":
                    instance.loader = "neoforge"
                
                case _:  # satisfy pyright
                    pass

    open_args = get_runner_args(args.runner, runner_path)
    stdin = BytesIO()

    selected_raw = 0
    # if this is a zipapp these paths won't exist after the importlib ctx exits, so we use an
    # exitstack to keep them alive whilst we run rofi.
    # in a regular pipx install, this doesn't matter.
    with contextlib.ExitStack() as stack:
        for instance in instances:
            icon_path = stack.enter_context(instance.real_icon_path)
            write_instance_string(runner, stdin, instance, icon_path)

        result = subprocess.run(
            args=open_args,
            input=stdin.getbuffer(),
            check=True,
            capture_output=True,
        )

        selected_raw = int(result.stdout[:-1].decode())

    # dmenu output is the number selected.
    selected = instances[selected_raw].name

    # execvp doesn't... seem to work?
    # prism just loads the main screen instead.
    subprocess.run([args.prism, "--launch", selected])


if __name__ == "__main__":
    main()
