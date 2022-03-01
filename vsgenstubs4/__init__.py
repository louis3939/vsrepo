#!/usr/bin/env python3
# -*- encoding: utf-8 -*-

import argparse
import inspect
import keyword
import os
import re
import sys
import vapoursynth
from typing import Any, Dict, List, Optional, Sequence, NamedTuple, Union, cast

parser = argparse.ArgumentParser()
parser.add_argument("plugins", type=str, nargs="*",
                    help="Only generate stubs for and inject specified plugin namespaces.")
parser.add_argument("--load-plugin", "-p", metavar="VS_PLUGIN", action="append",
                    help="Load non-auto-loaded VapourSynth plugin.")
parser.add_argument("--avs-plugin", "-a", action="append", help="Load AviSynth plugin.")
parser.add_argument("--output", "-o",
                    help="Where to output the stub package."
                         " By default, will attempt to install in site-packages alongside VapourSynth.")
parser.add_argument("--pyi-template", default=os.path.join(os.path.dirname(__file__), "_vapoursynth.part.pyi"),
                    help="Don't use unless you know what you are doing.")

CoreLike = Union[vapoursynth.Core, vapoursynth.VideoNode, vapoursynth.AudioNode]


class PluginMeta(NamedTuple):
    name: str
    description: str
    bound: Dict[str, Sequence[str]]


class Implementation(NamedTuple):
    name: str
    classes: List[str]


class Instance(NamedTuple):
    name: str
    bound: List[str]


def prepare_cores(ns: argparse.Namespace) -> vapoursynth.Core:
    core = vapoursynth.core.core
    if ns.load_plugin:
        for plugin in ns.load_plugin:
            core.std.LoadPlugin(os.path.abspath(plugin))

    if ns.avs_plugin:
        for plugin in ns.avs_plugin:
            if hasattr(core, "avs"):
                cast(Any, core).avs.LoadPlugin(os.path.abspath(plugin))
            else:
                raise AttributeError("Core is missing avs plugin!")

    return core


def retrieve_ns_and_funcs(core: vapoursynth.Core, *,
                          plugins: Optional[Sequence[str]] = None) -> Sequence[PluginMeta]:
    result = []

    for p in core.plugins():
        if plugins and p.namespace not in plugins:
            continue
        bound = {}
        cores: Sequence[CoreLike] = (core, core.std.BlankClip(), core.std.BlankAudio())
        for c in cores:
            sigs = retrieve_func_sigs(c, p.namespace)
            if sigs:
                bound[c.__class__.__name__] = sigs
        result.append(PluginMeta(p.namespace, p.name, bound))

    return result


def retrieve_func_sigs(core: CoreLike, ns: str) -> Sequence[str]:
    result = []
    plugin = getattr(core, ns)
    for func in plugin.functions():
        if func.name in dir(plugin):
            try:
                signature = inspect.signature(getattr(plugin, func.name))
            except BaseException:
                signature = inspect.Signature(
                    [inspect.Parameter('args', inspect.Parameter.VAR_POSITIONAL, annotation=Any),
                     inspect.Parameter('kwargs', inspect.Parameter.VAR_KEYWORD, annotation=Any)],
                    return_annotation=Optional[vapoursynth.VideoNode]
                )

            if signature.return_annotation in {Any, Optional[Any]}:
                signature = signature.replace(return_annotation=vapoursynth.VideoNode)

            sig = str(signature)

            # Clean up the type annotations so that they are valid python syntax.
            sig = sig.replace("Union", "typing.Union").replace("Sequence", "typing.Sequence")
            sig = sig.replace("vapoursynth.", "")
            sig = sig.replace("VideoNode", '"VideoNode"').replace("VideoFrame", '"VideoFrame"')
            sig = sig.replace("AudioNode", '"AudioNode"').replace("AudioFrame", '"AudioFrame"')
            sig = sig.replace("Any", "typing.Any")
            sig = sig.replace("NoneType", "None")
            sig = sig.replace("Optional", "typing.Optional")
            sig = sig.replace("Tuple", "typing.Tuple")

            # Make Callable definitions sensible
            sig = sig.replace("typing.Union[Func, Callable]", "typing.Callable[..., typing.Any]")
            sig = sig.replace("typing.Union[Func, Callable, None]", "typing.Optional[typing.Callable[..., typing.Any]]")

            # Replace the keywords with valid values
            for kw in keyword.kwlist:
                sig = sig.replace(f" {kw}:", f" {kw}_:")

            # Add a self.
            sig = sig.replace("(", "(self, ").replace(", )", ")")
            result.append(f"    def {func.name}{sig}: ...")
    return result


def make_implementations(sigs: Sequence[PluginMeta]) -> Dict[str, Implementation]:
    result: Dict[str, Implementation] = {}
    for s in sigs:
        c = [f"# implementation: {s.name}"]
        for k, v in s.bound.items():
            c += [
                "",
                f"class _Plugin_{s.name}_{k}_Bound(Plugin):",
                '    """',
                '    This class implements the module definitions for the corresponding VapourSynth plugin.',
                '    This class cannot be imported.',
                '    """',
                "\n".join(v),
                "",
            ]
        c += ["# end implementation"]
        result[s.name] = Implementation(s.name, c)
    return result


def make_instances(sigs: Sequence[PluginMeta]) -> Dict[str, Dict[str, Instance]]:
    result: Dict[str, Dict[str, Instance]] = {}
    for s in sigs:
        for k, v in s.bound.items():
            if v:
                if k not in result:
                    result[k] = {}
                bound = [
                    f"# instance_bound_{k}: {s.name}",
                    "    @property",
                    f"    def {s.name}(self) -> _Plugin_{s.name}_{k}_Bound:",
                    '        """',
                    f'        {s.description}',
                    '        """',
                    "# end instance",
                ]
                result[k][s.name] = Instance(s.name, bound)
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


def get_existing_instances(path: str) -> Dict[str, Dict[str, Instance]]:
    result: Dict[str, Dict[str, Instance]] = {}
    bound: Optional[str]
    bound_pattern = re.compile("^# instance_bound_([^:]+): (.+)")
    with open(path, "r") as f:
        current_instance: Optional[str] = None
        for line in f:
            line = line.rstrip()
            if bound_pattern.match(line):
                obj, current_instance = bound_pattern.findall(line)[0]
                assert current_instance
                if obj not in result:
                    result[obj] = {}
                if current_instance not in result[obj]:
                    result[obj][current_instance] = Instance(current_instance, [])
            if current_instance:
                result[obj][current_instance].bound.append(line)
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

    install_dir = args.output if args.output \
        else os.path.join(os.path.dirname(vapoursynth.__file__), "vapoursynth-stubs")
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

        for obj in existing_instances:
            if obj not in instances:
                instances[obj] = {}
            for plug in existing_instances[obj]:
                if plug not in instances[obj]:
                    instances[obj][plug] = existing_instances[obj][plug]

    with open(args.pyi_template) as f:
        template = f.read()

    implementation_inject = "\n\n\n".join(["\n".join(x.classes)
                                           for x in sorted(implementations.values(), key=lambda i: i.name)])

    template = template.replace("#include <plugins/implementations>", implementation_inject)

    for obj, instance in instances.items():
        inject = "\n".join(["\n".join(x.bound) for x in sorted(instance.values(), key=lambda i: i.name)])
        template = template.replace(f"#include <plugins/bound/{obj}>", inject)

    install_stub_package(install_dir, template)


if __name__ == "__main__":
    main(sys.argv[1:])
