#!/usr/bin/env python3
# -*- encoding: utf-8 -*-

import os
import sys
import inspect
import argparse
import keyword
import vapoursynth
from typing import Dict, List, Optional, Sequence, Union, NamedTuple

parser = argparse.ArgumentParser()
parser.add_argument("plugins", type=str, nargs="*", help="Only generate stubs for and inject specified plugin namespaces.")
parser.add_argument("--load-plugin", "-p", metavar="VS_PLUGIN", action="append", help="Load non-auto-loaded VapourSynth plugin.")
parser.add_argument("--avs-plugin", "-a", action="append", help="Load AviSynth plugin.")
parser.add_argument("--output", "-o", help="Where to output the stub package. By default, will attempt to install in site-packages alongside VapourSynth.")
parser.add_argument("--pyi-template", default=os.path.join(os.path.dirname(__file__), "_vapoursynth.part.pyi"), help="Don't use unless you know what you are doing.")


class PluginMeta(NamedTuple):
    name: str
    description: str
    unbound: List[str]
    bound: List[str]


class Implementation(NamedTuple):
    name: str
    classes: List[str]


class Instance(NamedTuple):
    name: str
    unbound: List[str]
    bound: List[str]


def prepare_cores(ns: argparse.Namespace) -> vapoursynth.Core:
    core = vapoursynth.core.core
    if ns.load_plugin:
        for plugin in ns.load_plugin:
            core.std.LoadPlugin(os.path.abspath(plugin))

    if ns.avs_plugin:
        for plugin in ns.avs_plugin:
            core.avs.LoadPlugin(os.path.abspath(plugin))

    return core


def retrieve_ns_and_funcs(core: vapoursynth.Core, *,
                          plugins: Optional[List[str]] = None) -> List[PluginMeta]:
    result = []

    for v in core.get_plugins().values():
        if plugins and v["namespace"] not in plugins:
            continue
        unbound_sigs = retrieve_func_sigs(core, v["namespace"], v["functions"].keys())
        bound_sigs = retrieve_func_sigs(core.std.BlankClip(), v["namespace"], v["functions"].keys())
        result.append(PluginMeta(v["namespace"], v["name"], unbound_sigs, bound_sigs))

    return result


def retrieve_func_sigs(core: Union[vapoursynth.Core, vapoursynth.VideoNode], ns: str, funcs: Sequence[str]) -> List[str]:
    result = []
    plugin = getattr(core, ns)
    for func in funcs:
        try:
            signature = str(inspect.signature(getattr(plugin, func)))
        except BaseException:
            signature = '(*args: typing.Any, **kwargs: typing.Any) -> Optional[VideoNode]'

        # Clean up the type annotations so that they are valid python syntax.
        signature = signature.replace("Union", "typing.Union").replace("Sequence", "typing.Sequence")
        signature = signature.replace("vapoursynth.", "")
        signature = signature.replace("VideoNode", '"VideoNode"').replace("VideoFrame", '"VideoFrame"')
        signature = signature.replace("NoneType", "None")
        signature = signature.replace("Optional", "typing.Optional")

        # Make Callable definitions sensible
        signature = signature.replace("typing.Union[Func, Callable]", "typing.Callable[..., typing.Any]")
        signature = signature.replace("typing.Union[Func, Callable, None]", "typing.Optional[typing.Callable[..., typing.Any]]")

        # Replace the keywords with valid values
        for kw in keyword.kwlist:
            signature = signature.replace(f" {kw}:", f" {kw}_:")

        # Add a self.
        signature = signature.replace("(", "(self, ").replace(", )", ")")
        result.append(f"    def {func}{signature}: ...")
    return result


def make_implementations(sigs: List[PluginMeta]) -> Dict[str, Implementation]:
    result: Dict[str, Implementation] = {}
    for s in sigs:
        c = [
            f"# implementation: {s.name}",
            f"class _Plugin_{s.name}_Unbound(Plugin):",
            '    """',
            '    This class implements the module definitions for the corresponding VapourSynth plugin.',
            '    This class cannot be imported.',
            '    """',
            "\n".join(s.unbound),
            "",
            "",
            f"class _Plugin_{s.name}_Bound(Plugin):",
            '    """',
            '    This class implements the module definitions for the corresponding VapourSynth plugin.',
            '    This class cannot be imported.',
            '    """',
            "\n".join(s.bound),
            "# end implementation",
        ]
        result[s.name] = Implementation(s.name, c)
    return result


def make_instances(sigs: List[PluginMeta]) -> Dict[str, Instance]:
    result: Dict[str, Instance] = {}
    for s in sigs:
        unbound = [
            f"# instance_unbound: {s.name}",
            "    @property",
            f"    def {s.name}(self) -> _Plugin_{s.name}_Unbound:",
            '        """',
            f'        {s.description}',
            '        """',
            f"# end instance",
        ]
        bound = [
            f"# instance_bound: {s.name}",
            "    @property",
            f"    def {s.name}(self) -> _Plugin_{s.name}_Bound:",
            '        """',
            f'        {s.description}',
            '        """',
            f"# end instance",
        ]
        result[s.name] = Instance(s.name, unbound, bound)
    return result


def get_existing_implementations(path: str) -> Dict[str, Implementation]:
    result: Dict[str, Implementation] = {}
    with open(path, "r") as f:
        current_imp: Optional[str] = None
        for line in f:
            line = line.rstrip()
            if line.startswith("# implementation: "):
                current_imp = line[len("# implementation: "):]
                result[current_imp] = Implementation(current_imp, [])
            if current_imp:
                result[current_imp].classes.append(line)
            if line.startswith("# end implementation"):
                current_imp = None

    return result


def get_existing_instances(path: str) -> Dict[str, Instance]:
    result: Dict[str, Instance] = {}
    with open(path, "r") as f:
        current_instance: Optional[str] = None
        for line in f:
            line = line.rstrip()
            if line.startswith("# instance_unbound: "):
                current_instance = line[len("# instance_unbound: "):]
                bound = False
                if current_instance not in result:
                    result[current_instance] = Instance(current_instance, [], [])
            if line.startswith("# instance_bound: "):
                current_instance = line[len("# instance_bound: "):]
                bound = True
                if current_instance not in result:
                    result[current_instance] = Instance(current_instance, [], [])
            if current_instance:
                if bound:
                    result[current_instance].bound.append(line)
                else:
                    result[current_instance].unbound.append(line)
            if line.startswith("# end instance"):
                current_instance = None

    return result


def write_init(path: str) -> None:
    with open(path, "w") as f:
        f.write("# flake8: noqa\n")
        f.write("\n")
        f.write("from .vapoursynth import *\n")


def inject_stub_package(stub_dir: str) -> None:
    site_package_dir = os.path.normpath(os.path.join(stub_dir, ".."))
    for iname in os.listdir(site_package_dir):
        if iname.startswith("VapourSynth-") and iname.endswith(".dist-info"):
            break
    else:
        return

    with open(os.path.join(site_package_dir, iname, "RECORD"), "a+", newline="") as f:
        f.seek(0)
        contents = f.read()
        if "__init__.pyi" not in contents:
            f.seek(0, os.SEEK_END)
            if not contents.endswith("\n"):
                f.write("\n")
            f.write("vapoursynth-stubs/__init__.pyi,,\n")


def install_stub_package(stub_dir: str, template: str) -> None:
    if not os.path.exists(stub_dir):
        os.makedirs(stub_dir)
    write_init(os.path.join(stub_dir, "__init__.pyi"))
    with open(os.path.join(stub_dir, "vapoursynth.pyi"), "w") as f:
        f.write(template)
        f.flush()
    inject_stub_package(stub_dir)


def main(argv: Optional[List[str]] = None) -> None:
    if argv is None:
        argv = sys.argv[1:]

    args = parser.parse_args(args=argv)
    core = prepare_cores(args)

    install_dir = args.output if args.output else os.path.join(os.path.dirname(vapoursynth.__file__), "vapoursynth-stubs")
    if not os.path.exists(install_dir):
        os.makedirs(install_dir)

    sigs = retrieve_ns_and_funcs(core, plugins=args.plugins)

    implementations = make_implementations(sigs)

    instances = make_instances(sigs)

    if os.path.isfile(os.path.join(install_dir, "vapoursynth.pyi")):
        existing_implementations = get_existing_implementations(os.path.join(install_dir, "vapoursynth.pyi"))
        existing_implementations.update(implementations)
        implementations = existing_implementations

        existing_instances = get_existing_instances(os.path.join(install_dir, "vapoursynth.pyi"))
        existing_instances.update(instances)
        instances = existing_instances

    with open(args.pyi_template) as f:
        template = f.read()

    implementation_inject = "\n\n\n".join(["\n".join(x.classes) for x in sorted(implementations.values(), key=lambda i: i.name)])
    instance_unbound_inject = "\n".join(["\n".join(x.unbound) for x in sorted(instances.values(), key=lambda i: i.name)])
    instance_bound_inject = "\n".join(["\n".join(x.bound) for x in sorted(instances.values(), key=lambda i: i.name)])

    template = template.replace("#include <plugins/implementations>", implementation_inject)
    template = template.replace("#include <plugins/unbound>", instance_unbound_inject)
    template = template.replace("#include <plugins/bound>", instance_bound_inject)

    install_stub_package(install_dir, template)


if __name__ == "__main__":
    main(sys.argv[1:])
