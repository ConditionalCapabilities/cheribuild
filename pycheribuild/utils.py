#
# Copyright (c) 2016 Alex Richardson
# All rights reserved.
#
# This software was developed by SRI International and the University of
# Cambridge Computer Laboratory under DARPA/AFRL contract FA8750-10-C-0237
# ("CTSRD"), as part of the DARPA CRASH research programme.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR AND CONTRIBUTORS ``AS IS'' AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
# OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
# SUCH DAMAGE.
#
import contextlib
import functools
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import typing
from pathlib import Path
from typing import Callable, Optional, Union

from .colour import AnsiColour, coloured

# reduce the number of import statements per project
__all__ = [
    "AnsiColour",
    "ConfigBase",
    "DoNotUseInIfStmt",
    "InstallInstructions",
    "OSInfo",
    "SafeDict",
    "SocketAndPort",
    "ThreadJoiner",
    "Type_T",
    "add_error_context",
    "classproperty",
    "coloured",
    "default_make_jobs_count",
    "error_message",
    "fatal_error",
    "final",
    "find_free_port",
    "get_global_config",
    "have_working_internet_connection",
    "include_local_file",
    "init_global_config",
    "is_case_sensitive_dir",
    "is_jenkins_build",
    "query_yes_no",
    "remove_duplicates",
    "remove_prefix",
    "remove_tuple_duplicates",
    "replace_one",
    "status_update",
    "typing",
    "warning_message",
]

if sys.version_info < (3, 8, 0):  # noqa: UP036
    sys.exit("This script requires at least Python 3.8.0")

Type_T = typing.TypeVar("Type_T")

try:
    from typing import final
except ImportError:

    def final(f: Type_T) -> Type_T:
        return f


# noinspection PyPep8Naming
class classproperty(typing.Generic[Type_T]):  # noqa: N801
    def __init__(self, f: "Callable[[typing.Any], Type_T]") -> None:
        self.f = f

    def __get__(self, obj, owner) -> Type_T:
        return self.f(owner)


# Placeholder until config has been initialized.
if typing.TYPE_CHECKING:
    DoNotUseInIfStmt = bool
else:

    class DoNotUseInIfStmt:
        def __bool__(self) -> "typing.NoReturn":
            raise ValueError("Should not be used")

        def __len__(self) -> "typing.NoReturn":
            raise ValueError("Should not be used")


class ConfigBase:
    TEST_MODE: bool = False

    def __init__(self, *, pretend: bool, verbose: bool, quiet: bool, force: bool) -> None:
        self.quiet = quiet
        self.verbose = verbose
        self.pretend = pretend
        self.force = force
        self.presume_connectivity = False
        self.internet_connection_last_checked_at: "Optional[float]" = None
        self.internet_connection_last_check_result = False


GlobalConfig: ConfigBase = ConfigBase(
    pretend=DoNotUseInIfStmt(), verbose=DoNotUseInIfStmt(), quiet=DoNotUseInIfStmt(), force=DoNotUseInIfStmt()
)


def init_global_config(config: ConfigBase, *, test_mode: bool = False) -> None:
    global GlobalConfig  # noqa: PLW0603
    GlobalConfig = config
    GlobalConfig.TEST_MODE = test_mode
    assert not (GlobalConfig.verbose and GlobalConfig.quiet), "mutually exclusive"


def get_global_config() -> ConfigBase:
    return GlobalConfig


def is_jenkins_build() -> bool:
    return os.getenv("_CHERIBUILD_JENKINS_BUILD") is not None


class SocketAndPort:
    def __init__(self, sock: socket.socket, port: int):
        self.socket = sock
        self.port = port


def find_free_port(preferred_port: "Optional[int]" = None) -> SocketAndPort:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if preferred_port is not None:
        try:
            s.bind(("127.0.0.1", preferred_port))
            return SocketAndPort(s, s.getsockname()[1])
        except OSError as e:
            import errno

            if e.errno != errno.EADDRINUSE:
                warning_message("Got unexpected error when checking whether port", preferred_port, "is free:", e)
            status_update("Port", preferred_port, "is not available, falling back to using a random port")
    s.bind(("localhost", 0))
    return SocketAndPort(s, s.getsockname()[1])


def default_make_jobs_count() -> Optional[int]:
    make_jobs = os.cpu_count()
    if make_jobs > 24:
        # don't use up all the resources on shared build systems
        # (you can still override this with the -j command line option)
        make_jobs //= 2
    return make_jobs


def maybe_add_space(msg: str, sep: str) -> "tuple[str, ...]":
    if not sep:
        return msg, " "
    return (msg,)


def status_update(*args, sep=" ", **kwargs) -> None:
    print(coloured(AnsiColour.cyan, *args, sep=sep), **kwargs)


def fixit_message(*args, sep=" ") -> None:
    print(
        coloured(AnsiColour.blue, maybe_add_space("Possible solution:", sep) + args, sep=sep),
        file=sys.stderr,
        flush=True,
    )


def warning_message(*args, sep=" ", fixit_hint=None) -> None:
    # we ignore fatal errors when simulating a run
    print(coloured(AnsiColour.magenta, maybe_add_space("Warning:", sep) + args, sep=sep), file=sys.stderr, flush=True)
    if fixit_hint:
        fixit_message(fixit_hint)


_ERROR_CONTEXT: "list[str]" = []


@contextlib.contextmanager
def add_error_context(context: str):
    _ERROR_CONTEXT.append(context)
    try:
        yield
        # We don't pop the error context if there is an exception so that we can print the context in the
        # except clause of main()
        _ERROR_CONTEXT.pop()
    except Exception as e:
        # print("Got exception in error context", context, e)
        raise e


def _add_error_context(prefix, args, sep) -> "str":
    if _ERROR_CONTEXT:
        # _ERROR_CONTEXT might contain escape sequences so we have to reset to red afterwards
        return coloured(
            AnsiColour.red,
            maybe_add_space(prefix + " " + _ERROR_CONTEXT[-1] + AnsiColour.red.escape_sequence() + ":", sep) + args,
            sep=sep,
        )
    return coloured(AnsiColour.red, maybe_add_space(prefix + ":", sep) + args, sep=sep)


def error_message(*args, sep=" ", fixit_hint=None) -> None:
    # we ignore fatal errors when simulating a run
    print(_add_error_context("Error", args, sep=sep), file=sys.stderr, flush=True)
    if fixit_hint:
        fixit_message(fixit_hint)


def fatal_error(*args, sep=" ", fixit_hint=None, fatal_when_pretending=False, exit_code=3, pretend: bool) -> None:
    # we ignore fatal errors when simulating a run
    if pretend:
        print(_add_error_context("Potential fatal error", args, sep=sep), file=sys.stderr, flush=True)
        if fixit_hint:
            fixit_message(fixit_hint)
        if fatal_when_pretending:
            traceback.print_stack()
            sys.exit(exit_code)
    else:
        print(_add_error_context("Fatal error", args, sep=sep), file=sys.stderr, flush=True)
        if fixit_hint:
            fixit_message(fixit_hint)
        sys.exit(exit_code)


def query_yes_no(
    config: ConfigBase,
    message: str = "",
    *,
    default_result=False,
    force_result=True,
    yes_no_str: "Optional[str]" = None,
) -> bool:
    if yes_no_str is None:
        yes_no_str = " [Y]/n " if default_result else " y/[N] "
    if config.pretend:
        print(message + yes_no_str, coloured(AnsiColour.green, "y" if force_result else "n"), sep="", flush=True)
        return force_result  # in pretend mode we always return true
    if config.force:
        # in force mode we always return the forced result without prompting the user
        print(message + yes_no_str, coloured(AnsiColour.green, "y" if force_result else "n"), sep="", flush=True)
        return force_result
    if not sys.__stdin__.isatty():
        return default_result  # can't get any input -> return the default
    result = input(message + yes_no_str)
    if default_result:
        return not result.startswith("n")  # if default is yes accept anything other than strings starting with "n"
    return str(result).lower().startswith("y")  # anything but y will be treated as false


@functools.lru_cache(maxsize=20)
def include_local_file(path: str) -> str:
    file = Path(__file__).parent / path
    if not file.is_file():
        fatal_error(file, "is missing!", pretend=False)
    with file.open("r", encoding="utf-8") as f:
        return f.read()


def have_working_internet_connection(config: ConfigBase) -> bool:
    if config.TEST_MODE or config.presume_connectivity:
        return True
    current_check_time = time.time()
    if config.internet_connection_last_checked_at:
        if current_check_time < config.internet_connection_last_checked_at + 60.0:
            # Assume that the detected values remains the same for 60 seconds to avoid repeated checks.
            # This saves around 50ms startup time.
            return config.internet_connection_last_check_result
    # Try to connect to google DNS server at 8.8.8.8 to check if we have a working internet connection
    # Don't make a DNS request since that could be broken for other reasons!
    # From https://stackoverflow.com/questions/3764291/checking-network-connection/33117579#33117579
    host = "8.8.8.8"
    port = 53
    timeout = 3
    x = None
    result = False
    try:
        x = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        x.settimeout(timeout)
        x.connect((host, port))
        result = True
    except OSError:
        result = False
    except Exception as ex:
        fatal_error("Something went wrong while checking for internet connection", ex, pretend=config.pretend)
        result = False
    finally:
        if x:
            x.close()
        config.internet_connection_last_check_result = result
        config.internet_connection_last_checked_at = current_check_time
        return result


def is_case_sensitive_dir(d: Path) -> bool:
    if not d.exists():
        # assume true for macos:
        if OSInfo.IS_MAC:
            return False
        return True  # XXX: exception?
    path_upper = d / "TestDirCaseSensitive"
    path_lower = d / "testdircasesensitive"
    if path_upper.exists():
        path_upper.rmdir()
    if path_lower.exists():
        path_lower.rmdir()
    path_upper.mkdir()
    if path_lower.exists():
        # Lowercase dir found -> case insensitive
        path_lower.rmdir()
        return False
    path_upper.rmdir()
    return True


class InstallInstructions:
    def __init__(
        self,
        message: "Union[str, Callable[[], str]]",
        cheribuild_target: "Optional[str]" = None,
        alternative: "Optional[str]" = None,
    ):
        self._message = message
        self.cheribuild_target = cheribuild_target
        self.alternative = alternative

    def fixit_hint(self) -> str:
        if callable(self._message):
            result = self._message()
        else:
            result = self._message
        if self.cheribuild_target:
            if result:
                result += "\nYou can also try running "
            else:
                result = "Run "
            result += "`cheribuild.py " + self.cheribuild_target + "` to install locally."
        if self.alternative:
            assert result, "Can't have an alternative without a default option!"
            result += "\nAlternatively " + self.alternative
        return result

    @classmethod
    def cmake(cls):
        return OSInfo.install_instructions(
            "cmake",
            False,
            default="cmake",
            homebrew="cmake",
            zypper="cmake",
            apt="cmake",
            freebsd="cmake",
            cheribuild_target="cmake",
        )


class OSInfo:
    IS_LINUX: bool = sys.platform.startswith("linux")
    IS_FREEBSD: bool = sys.platform.startswith("freebsd")
    IS_MAC: bool = sys.platform.startswith("darwin")
    __os_release_cache: "Optional[dict[str, str]]" = None

    @classmethod
    def is_ubuntu(cls) -> bool:
        return cls.__is_linux_distribution("ubuntu")

    @classmethod
    def is_suse(cls) -> bool:
        return cls.__is_linux_distribution("suse") or cls.__is_linux_distribution("opensuse")

    @classmethod
    def is_debian(cls) -> bool:
        return cls.__is_linux_distribution("debian")

    @classmethod
    def is_cheribsd(cls) -> bool:
        return cls.IS_FREEBSD and cls.etc_os_release().get("ID", "") == "cheribsd"

    @classmethod
    def __is_linux_distribution(cls, kind):
        if not cls.IS_LINUX:
            return False
        return kind in cls.etc_os_release().get("ID", "") or kind in cls.etc_os_release().get("ID_LIKE", "")

    @staticmethod
    def etc_os_release() -> "dict[str, str]":
        if OSInfo.__os_release_cache is None:
            OSInfo.__os_release_cache = OSInfo.__parse_etc_os_release()
        return OSInfo.__os_release_cache

    @staticmethod
    def __parse_etc_os_release() -> "dict[str, str]":
        if not Path("/etc/os-release").exists():
            return {}
        with Path("/etc/os-release").open(encoding="utf-8") as f:
            d = {}
            for line in f:
                line = line.strip()
                if not line or line[0] == "#":
                    continue
                k, v = line.split("=", maxsplit=1)
                # .strip('"') will remove if there or else do nothing
                d[k] = v.strip('"')
        return d

    @classmethod
    def package_manager(cls, compat_abi=False) -> str:
        if cls.IS_MAC:
            return "brew"
        elif cls.IS_FREEBSD:
            if cls.is_cheribsd():
                return "pkg64" if compat_abi else "pkg64c"
            return "pkg"
        elif cls.IS_LINUX:
            if cls.uses_zypper():
                return "zypper"
            elif cls.uses_apt():
                return "apt"
        return "<system package manager>"

    @classmethod
    def install_instructions(
        cls,
        name,
        is_lib,
        default=None,
        homebrew=None,
        apt=None,
        zypper=None,
        freebsd=None,
        cheribuild_target=None,
        alternative=None,
        compat_abi=False,
    ) -> InstallInstructions:
        guessed_package = False
        if cls.IS_MAC and homebrew:
            install_name = homebrew
        elif cls.IS_FREEBSD and freebsd:
            install_name = freebsd
        elif cls.uses_apt():
            if apt:
                install_name = apt
            else:
                guessed_package = True
                install_name = "lib" + name + "-dev" if is_lib else name
        elif cls.uses_zypper():
            if zypper:
                install_name = zypper
            else:
                if not is_lib and shutil.which("command-not-found"):
                    # for programs we can use the command-not-found tool to get detailed install instructions
                    def command_not_found():
                        hint = subprocess.getoutput(shutil.which("command-not-found") + " " + name)
                        print(hint)
                        if hint and name + ": command not found" not in hint:
                            msg_start = hint.find("The program")
                            if msg_start:
                                hint = hint[msg_start:]
                            return hint
                        return f"Could not find package for program {name}. Maybe `zypper in {name}` will work."

                    return InstallInstructions(command_not_found, cheribuild_target, alternative)
                guessed_package = True
                install_name = "lib" + name + "-devel" if is_lib else name
        else:
            guessed_package = True
            install_name = name
        if guessed_package and default:
            guessed_package = False
            install_name = default

        if guessed_package:
            # not sure if the package name is correct:
            return InstallInstructions(
                f"Possibly running `{cls.package_manager(compat_abi)} install {install_name}"
                "` fixes this. Note: package name may not be correct.",
                cheribuild_target,
                alternative,
            )
        else:
            return InstallInstructions(
                f"Run `{cls.package_manager(compat_abi)} install " + install_name + "`", cheribuild_target, alternative
            )

    @classmethod
    def uses_apt(cls) -> bool:
        return cls.is_debian() or cls.is_ubuntu()

    @classmethod
    def uses_zypper(cls) -> bool:
        return cls.is_suse()


class ThreadJoiner:
    def __init__(self, threads: "Optional[Union[threading.Thread, list[threading.Thread]]]" = None):
        if not isinstance(threads, list):
            if threads is None:
                self.threads = []
            else:
                self.threads = [threads]
        else:
            self.threads = threads

    def __enter__(self) -> None:
        for thread in self.threads:
            thread.start()

    def __exit__(self, *args) -> None:
        for thread in self.threads:
            if thread.is_alive():
                status_update("Waiting for '", thread.name, "' to complete", sep="")
            thread.join()

    def __add__(self, other: "ThreadJoiner") -> "ThreadJoiner":
        return ThreadJoiner(self.threads + other.threads)

    def __iadd__(self, other: "ThreadJoiner") -> "ThreadJoiner":
        self.threads += other.threads
        return self


def replace_one(s: str, old, new) -> str:
    """Like str.replace() but raises an exception if old is not in s"""
    result = s.replace(old, new, 1)
    if result == s:
        raise ValueError(old + " not contained in " + s)
    return result


def remove_duplicates(items: "typing.Iterable[Type_T]") -> "list[Type_T]":
    # Convert to a dict to remove duplicates (retains order since python 3.6, which is older than our minimum)
    return list(dict.fromkeys(items))


def remove_tuple_duplicates(items: "typing.Iterable[Type_T]") -> "tuple[Type_T, ...]":
    # Convert to a dict to remove duplicates (retains order since python 3.6, which is older than our minimum)
    return tuple(dict.fromkeys(items))


def remove_prefix(s: str, prefix: str, prefix_required=False) -> str:
    if not s.startswith(prefix):
        if prefix_required:
            raise ValueError(s + " does not start with " + prefix)
        return s
    return s[len(prefix) :]


# A dictionary for string formatting (format_map) that preserves values not
# provided for later expansion
#
# https://stackoverflow.com/questions/17215400/python-format-string-unused-named-arguments
class SafeDict(dict):
    def __missing__(self, key) -> str:
        return "{" + key + "}"
