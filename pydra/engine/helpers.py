"""Administrative support for the engine framework."""
import asyncio
import asyncio.subprocess as asp
import itertools
import inspect

# import abc
from pathlib import Path
import os
import sys
from uuid import uuid4
import subprocess as sp
import getpass
import re
from time import strftime
from traceback import format_exception
import typing as ty

# import inspect
# import warnings
import attr
from filelock import SoftFileLock, Timeout
import cloudpickle as cp


from .specs import (
    Runtime,
    File,
    Directory,
    attr_fields,
    Result,
    LazyField,
    MultiOutputObj,
    # MultiInputObj,
    # MultiInputFile,
    # MultiOutputFile,
)
from .helpers_file import hash_file, hash_dir, copyfile, is_existing_file
from ..utils.hash import hash_object


def ensure_list(obj, tuple2list=False):
    """
    Return a list whatever the input object is.

    Examples
    --------
    >>> ensure_list(list("abc"))
    ['a', 'b', 'c']
    >>> ensure_list("abc")
    ['abc']
    >>> ensure_list(tuple("abc"))
    [('a', 'b', 'c')]
    >>> ensure_list(tuple("abc"), tuple2list=True)
    ['a', 'b', 'c']
    >>> ensure_list(None)
    []
    >>> ensure_list(5.0)
    [5.0]

    """
    if obj is None:
        return []
    # list or numpy.array (this might need some extra flag in case an array has to be converted)
    elif isinstance(obj, list) or hasattr(obj, "__array__"):
        return obj
    elif tuple2list and isinstance(obj, tuple):
        return list(obj)
    elif isinstance(obj, list):
        return obj
    elif isinstance(obj, LazyField):
        return obj
    return [obj]


def print_help(obj):
    """Visit a task object and print its input/output interface."""
    lines = [f"Help for {obj.__class__.__name__}"]
    input_klass = make_klass(obj.input_spec)
    if attr.fields(input_klass):
        lines += ["Input Parameters:"]
    for f in attr.fields(input_klass):
        default = ""
        if f.default != attr.NOTHING and not f.name.startswith("_"):
            default = f" (default: {f.default})"
        try:
            name = f.type.__name__
        except AttributeError:
            name = str(f.type)
        lines += [f"- {f.name}: {name}{default}"]
    output_klass = make_klass(obj.output_spec)
    if attr.fields(output_klass):
        lines += ["Output Parameters:"]
    for f in attr.fields(output_klass):
        try:
            name = f.type.__name__
        except AttributeError:
            name = str(f.type)
        lines += [f"- {f.name}: {name}"]
    print("\n".join(lines))
    return lines


def load_result(checksum, cache_locations):
    """
    Restore a result from the cache.

    Parameters
    ----------
    checksum : :obj:`str`
        Unique identifier of the task to be loaded.
    cache_locations : :obj:`list` of :obj:`os.pathlike`
        List of cache directories, in order of priority, where
        the checksum will be looked for.

    """
    if not cache_locations:
        return None
    # TODO: if there are issues with loading, we might need to
    # TODO: sleep and repeat loads (after checking that there are no lock files!)
    for location in cache_locations:
        if (location / checksum).exists():
            result_file = location / checksum / "_result.pklz"
            if result_file.exists() and result_file.stat().st_size > 0:
                return cp.loads(result_file.read_bytes())
            return None
    return None


def save(task_path: Path, result=None, task=None, name_prefix=None):
    """
    Save a :class:`~pydra.engine.core.TaskBase` object and/or results.

    Parameters
    ----------
    task_path : :obj:`Path`
        Write directory
    result : :obj:`Result`
        Result to pickle and write
    task : :class:`~pydra.engine.core.TaskBase`
        Task to pickle and write
    """

    if task is None and result is None:
        raise ValueError("Nothing to be saved")

    if not isinstance(task_path, Path):
        task_path = Path(task_path)
    task_path.mkdir(parents=True, exist_ok=True)
    if name_prefix is None:
        name_prefix = ""

    lockfile = task_path.parent / (task_path.name + "_save.lock")
    with SoftFileLock(lockfile):
        if result:
            if task_path.name.startswith("Workflow") and result.output is not None:
                # copy files to the workflow directory
                result = copyfile_workflow(wf_path=task_path, result=result)
            with (task_path / f"{name_prefix}_result.pklz").open("wb") as fp:
                cp.dump(result, fp)
        if task:
            with (task_path / f"{name_prefix}_task.pklz").open("wb") as fp:
                cp.dump(task, fp)


def copyfile_workflow(wf_path, result):
    """if file in the wf results, the file will be copied to the workflow directory"""
    for field in attr_fields(result.output):
        value = getattr(result.output, field.name)
        # if the field is a path or it can contain a path _copyfile_single_value is run
        # to move all files and directories to the workflow directory
        if field.type in [File, Directory, MultiOutputObj] or type(value) in [
            list,
            tuple,
            dict,
        ]:
            new_value = _copyfile_single_value(wf_path=wf_path, value=value)
            setattr(result.output, field.name, new_value)
    return result


def _copyfile_single_value(wf_path, value):
    """checking a single value for files that need to be copied to the wf dir"""
    if isinstance(value, (tuple, list)):
        return [_copyfile_single_value(wf_path, val) for val in value]
    elif isinstance(value, dict):
        return {
            key: _copyfile_single_value(wf_path, val) for (key, val) in value.items()
        }
    elif is_existing_file(value):
        new_path = wf_path / Path(value).name
        copyfile(originalfile=value, newfile=new_path, copy=True, use_hardlink=True)
        return new_path
    else:
        return value


def task_hash(task):
    """
    Calculate the checksum of a task.

    input hash, output hash, environment hash

    Parameters
    ----------
    task : :class:`~pydra.engine.core.TaskBase`
        The input task.

    """
    return NotImplementedError


def gather_runtime_info(fname):
    """
    Extract runtime information from a file.

    Parameters
    ----------
    fname : :obj:`os.pathlike`
        The file containing runtime information

    Returns
    -------
    runtime : :obj:`Runtime`
        A runtime object containing the collected information.

    """
    runtime = Runtime(rss_peak_gb=None, vms_peak_gb=None, cpu_peak_percent=None)

    # Read .prof file in and set runtime values
    data = [
        [float(el) for el in line.strip().split(",")]
        for line in Path(fname).read_text().splitlines()
    ]
    if data:
        runtime.rss_peak_gb = max([val[2] for val in data]) / 1024
        runtime.vms_peak_gb = max([val[3] for val in data]) / 1024
        runtime.cpu_peak_percent = max([val[1] for val in data])

    """
    runtime.prof_dict = {
        'time': vals[:, 0].tolist(),
        'cpus': vals[:, 1].tolist(),
        'rss_GiB': (vals[:, 2] / 1024).tolist(),
        'vms_GiB': (vals[:, 3] / 1024).tolist(),
    }
    """
    return runtime


def make_klass(spec):
    """
    Create a data class given a spec.

    Parameters
    ----------
    spec :
        TODO

    """
    if spec is None:
        return None
    fields = spec.fields
    if fields:
        newfields = {}
        for item in fields:
            if len(item) == 2:
                name = item[0]
                if isinstance(item[1], attr._make._CountingAttr):
                    newfield = item[1]
                else:
                    newfield = attr.ib(type=item[1])
            else:
                if (
                    any([isinstance(ii, attr._make._CountingAttr) for ii in item])
                    or len(item) > 4
                ):
                    raise ValueError(
                        "syntax not valid, you can use (name, attr), "
                        "(name, type, default), (name, type, default, metadata)"
                        "or (name, type, metadata)"
                    )
                kwargs = {}
                if len(item) == 3:
                    name, tp = item[:2]
                    if isinstance(item[-1], dict) and "help_string" in item[-1]:
                        mdata = item[-1]
                        kwargs["metadata"] = mdata
                    else:
                        kwargs["default"] = item[-1]
                elif len(item) == 4:
                    name, tp, dflt, mdata = item
                    kwargs["default"] = dflt
                    kwargs["metadata"] = mdata
                newfield = attr.ib(
                    type=tp,
                    **kwargs,
                )
            newfield.converter = TypeCoercer[newfield.type](
                newfield.type,
                coercible=[
                    (os.PathLike, os.PathLike),
                    (str, os.PathLike),
                    (os.PathLike, str),
                    (ty.Sequence, ty.Sequence),
                    (ty.Mapping, ty.Mapping),
                ],
                not_coercible=[(str, ty.Sequence), (ty.Sequence, str)],
            )
            try:
                newfield.metadata["allowed_values"]
            except KeyError:
                pass
            else:
                newfield.validator = allowed_values_validator
            newfields[name] = newfield
        fields = newfields
    return attr.make_class(spec.name, fields, bases=spec.bases, kw_only=True)


T = ty.TypeVar("T")
TypeOrAny = ty.Union[type, ty.Any]


class TypeCoercer(ty.Generic[T]):
    """Coerces an object to the given type, expanding container classes and unions.

    Parameters
    ----------
    tp : type
        the type objects will be coerced to
    coercible: Iterable[tuple[type or Any, type or Any]], optional
        limits coercing between the pairs of types where they appear within the
        tree of more complex nested container types.
    not_coercible: Iterable[tuple[type or Any, type or Any]], optional
        excludes the limits coercing between the pairs of types where they appear within
        the tree of more complex nested container types. Overrides 'coercible' to enable
        you to carve out exceptions, such as
            TypeCoercer(list, coercible=[(ty.Iterable, list)], not_coercible=[(str, list)])
    """

    coercible: list[tuple[TypeOrAny, TypeOrAny]]
    not_coercible: list[tuple[TypeOrAny, TypeOrAny]]

    def __init__(
        self,
        tp,
        coercible: ty.Optional[ty.Iterable[tuple[TypeOrAny, TypeOrAny]]] = None,
        not_coercible: ty.Optional[ty.Iterable[tuple[TypeOrAny, TypeOrAny]]] = None,
    ):
        def expand(t):
            origin = ty.get_origin(t)
            if origin is None:
                return t
            args = ty.get_args(t)
            if not args or args == (Ellipsis,):
                assert isinstance(origin, type)
                return origin
            return (origin, [expand(a) for a in args])

        self.coercible = (
            list(coercible) if coercible is not None else [(ty.Any, ty.Any)]
        )
        self.not_coercible = list(not_coercible) if not_coercible is not None else []
        self.pattern = expand(tp)

    def __call__(self, object_: ty.Any) -> T:
        """Attempts to coerce

        Parameters
        ----------
        object_ : ty.Any
            the object to coerce

        Returns
        -------
        T
            the coerced object

        Raises
        ------
        TypeError
            if the coercion is not possible, or not specified by the `coercible`/`not_coercible`
            parameters, then a TypeError is raised
        """

        def expand_and_coerce(obj, pattern: ty.Union[type | tuple]):
            """Attempt to expand the object along the lines of the coercion pattern"""
            if not isinstance(pattern, tuple):
                return coerce_single(obj, pattern)
            origin, pattern_args = pattern
            if origin is ty.Union:
                # Return the first argument in the union that is coercible
                for arg in pattern_args:
                    try:
                        return expand_and_coerce(obj, arg)
                    except TypeError:
                        pass
                raise TypeError(
                    f"Could not coerce {obj} to any of the union types {pattern_args}"
                )
            if not self.is_instance(obj, origin):
                self._check_coercible(obj, origin)
                type_ = origin
            else:
                type_ = type(obj)
            if issubclass(type_, ty.Mapping):
                return coerce_mapping(obj, type_, pattern_args)
            return coerce_sequence(obj, type_, pattern_args)

        def coerce_single(obj, pattern):
            """Coerce a "single" object, i.e. one not nested within a container"""
            if (
                obj is attr.NOTHING
                or pattern is inspect._empty
                or self.is_instance(obj, pattern)
            ):
                return obj
            if isinstance(obj, LazyField):
                self._check_coercible(obj.type, pattern)
                return obj
            self._check_coercible(obj, pattern)
            return coerce_to_type(obj, pattern)

        def coerce_mapping(
            obj: ty.Mapping, type_: ty.Type[ty.Mapping], pattern_args: list
        ):
            """Coerce a mapping (e.g. dict)"""
            assert len(pattern_args) == 2
            try:
                items = obj.items()
            except AttributeError as e:
                msg = (
                    f" (part of coercion from {object_} to {self.pattern}"
                    if obj is not object_
                    else ""
                )
                raise TypeError(
                    f"Could not coerce to {type_} as {obj} is not a mapping type{msg}"
                ) from e
            return coerce_to_type(
                (
                    (
                        expand_and_coerce(k, pattern_args[0]),
                        expand_and_coerce(v, pattern_args[1]),
                    )
                    for k, v in items
                ),
                type_,
            )

        def coerce_sequence(
            obj: ty.Sequence, type_: ty.Type[ty.Sequence], pattern_args: list
        ):
            """Coerce a sequence object (e.g. list, tuple, ...)"""
            try:
                args = list(obj)
            except TypeError as e:
                msg = (
                    f" (part of coercion from {object_} to {self.pattern}"
                    if obj is not object_
                    else ""
                )
                raise TypeError(
                    f"Could not coerce to {type_} as {obj} is not iterable{msg}"
                ) from e
            if issubclass(type_, ty.Tuple):  # type: ignore[arg-type]
                if pattern_args[-1] is Ellipsis:
                    pattern_args = itertools.chain(
                        pattern_args[:-2], itertools.repeat(pattern_args[-2])
                    )
                elif len(pattern_args) != len(args):
                    raise TypeError(
                        f"Incorrect number of items in {obj}, expected "
                        f"{len(pattern_args)}, got {len(args)}"
                    )
                return coerce_to_type(
                    [expand_and_coerce(o, p) for o, p in zip(args, pattern_args)], type_
                )
            assert len(pattern_args) == 1
            return coerce_to_type(
                [expand_and_coerce(o, pattern_args[0]) for o in args], type_
            )

        def coerce_to_type(obj, type_):
            """Attempt to do the innermost (i.e. non-nested) coercion and fail with
            helpful message
            """
            try:
                return type_(obj)
            except TypeError as e:
                msg = (
                    f" (part of coercion from {object_} to {self.pattern}"
                    if obj is not object_
                    else ""
                )
                raise TypeError(f"Cannot coerce {obj} into {type_}{msg}") from e

        return expand_and_coerce(object_, self.pattern)

    def _check_coercible(self, source: object | type, target: type | ty.Any):
        """Checks whether the source object or type is coercible to the target type
        given the coercion rules defined in the `coercible` and `not_coercible` attrs

        Parameters
        ----------
        source : object | type
            source object or type to be coerced
        target : type | ty.Any
            target type for the source to be coerced to
        """

        source_check = (
            self.is_or_subclass if inspect.isclass(source) else self.is_instance
        )

        def matches(criteria):
            return [
                (src, tgt)
                for src, tgt in criteria
                if source_check(source, src) and self.is_or_subclass(target, tgt)
            ]

        if not matches(self.coercible):
            raise TypeError(
                f"Cannot coerce {source} into {target} as the coercion doesn't match "
                f"any of the explicit inclusion criteria {self.coercible}"
            )
        matches_not_coercible = matches(self.not_coercible)
        if matches_not_coercible:
            raise TypeError(
                f"Cannot coerce {source} into {target} as it is explicitly excluded by "
                f"the following coercion criteria {matches_not_coercible}"
            )

    @staticmethod
    def is_instance(obj, cls):
        """Checks whether the object is an instance of cls or that cls is typing.Any"""
        return cls is ty.Any or isinstance(obj, cls)

    @staticmethod
    def is_or_subclass(a, b):
        """Checks whether the class a is either the same as b, a subclass of b or b is
        typing.Any"""
        return a is b or b is ty.Any or issubclass(a, b)


# def custom_validator(instance, attribute, value):
#     """simple custom validation
#     take into account ty.Union, ty.List, ty.Dict (but only one level depth)
#     adding an additional validator, if allowe_values provided
#     """
#     validators = []
#     tp_attr = attribute.type
#     # a flag that could be changed to False, if the type is not recognized
#     check_type = True
#     if (
#         value is attr.NOTHING
#         or value is None
#         or attribute.name.startswith("_")  # e.g. _func
#         or isinstance(value, LazyField)
#         or tp_attr
#         in [
#             ty.Any,
#             inspect._empty,
#             MultiOutputObj,
#             MultiInputObj,
#             MultiOutputFile,
#             MultiInputFile,
#         ]
#     ):
#         check_type = False  # no checking of the type
#     elif isinstance(tp_attr, type) or tp_attr in [File, Directory]:
#         tp = _single_type_update(tp_attr, name=attribute.name)
#         cont_type = None
#     else:  # more complex types
#         cont_type, tp_attr_list = _check_special_type(tp_attr, name=attribute.name)
#         if cont_type is ty.Union:
#             tp, check_type = _types_updates(tp_attr_list, name=attribute.name)
#         elif cont_type is list:
#             tp, check_type = _types_updates(tp_attr_list, name=attribute.name)
#         elif cont_type is dict:
#             # assuming that it should have length of 2 for keys and values
#             if len(tp_attr_list) != 2:
#                 check_type = False
#             else:
#                 tp_attr_key, tp_attr_val = tp_attr_list
#             # updating types separately for keys and values
#             tp_k, check_k = _types_updates([tp_attr_key], name=attribute.name)
#             tp_v, check_v = _types_updates([tp_attr_val], name=attribute.name)
#             # assuming that I have to be able to check keys and values
#             if not (check_k and check_v):
#                 check_type = False
#             else:
#                 tp = {"key": tp_k, "val": tp_v}
#         else:
#             warnings.warn(
#                 f"no type check for {attribute.name} field, "
#                 f"no type check implemented for value {value} and type {tp_attr}"
#             )
#             check_type = False

#     if check_type:
#         validators.append(_type_validator(instance, attribute, value, tp, cont_type))

#     # checking additional requirements for values (e.g. allowed_values)
#     meta_attr = attribute.metadata
#     if "allowed_values" in meta_attr:
#         validators.append(_allowed_values_validator(isinstance, attribute, value))
#     return validators


# def _type_validator(instance, attribute, value, tp, cont_type):
#     """creating a customized type validator,
#     uses validator.deep_iterable/mapping if the field is a container
#     (i.e. ty.List or ty.Dict),
#     it also tries to guess when the value is a list due to the splitter
#     and validates the elements
#     """
#     if cont_type is None or cont_type is ty.Union:
#         # if tp is not (list,), we are assuming that the value is a list
#         # due to the splitter, so checking the member types
#         if isinstance(value, list) and tp != (list,):
#             return attr.validators.deep_iterable(
#                 member_validator=attr.validators.instance_of(
#                     tp + (attr._make._Nothing,)
#                 )
#             )(instance, attribute, value)
#         else:
#             return attr.validators.instance_of(tp + (attr._make._Nothing,))(
#                 instance, attribute, value
#             )
#     elif cont_type is list:
#         return attr.validators.deep_iterable(
#             member_validator=attr.validators.instance_of(tp + (attr._make._Nothing,))
#         )(instance, attribute, value)
#     elif cont_type is dict:
#         return attr.validators.deep_mapping(
#             key_validator=attr.validators.instance_of(tp["key"]),
#             value_validator=attr.validators.instance_of(
#                 tp["val"] + (attr._make._Nothing,)
#             ),
#         )(instance, attribute, value)
#     else:
#         raise Exception(
#             f"container type of {attribute.name} should be None, list, dict or ty.Union, "
#             f"and not {cont_type}"
#         )


# def _types_updates(tp_list, name):
#     """updating the type's tuple with possible additional types"""
#     tp_upd_list = []
#     check = True
#     for tp_el in tp_list:
#         tp_upd = _single_type_update(tp_el, name, simplify=True)
#         if tp_upd is None:
#             check = False
#             break
#         else:
#             tp_upd_list += list(tp_upd)
#     tp_upd = tuple(set(tp_upd_list))
#     return tp_upd, check


# def _single_type_update(tp, name, simplify=False):
#     """updating a single type with other related types - e.g. adding bytes for str
#     if simplify is True, than changing typing.List to list etc.
#     (assuming that I validate only one depth, so have to simplify at some point)
#     """
#     if isinstance(tp, type) or tp in [File, Directory]:
#         if tp is str:
#             return (str, bytes)
#         elif tp in [File, Directory, os.PathLike]:
#             return (os.PathLike, str)
#         elif tp is float:
#             return (float, int)
#         else:
#             return (tp,)
#     elif simplify is True:
#         warnings.warn(f"simplify validator for {name} field, checking only one depth")
#         cont_tp, types_list = _check_special_type(tp, name=name)
#         if cont_tp is list:
#             return (list,)
#         elif cont_tp is dict:
#             return (dict,)
#         elif cont_tp is ty.Union:
#             return types_list
#         else:
#             warnings.warn(
#                 f"no type check for {name} field, type check not implemented for type of {tp}"
#             )
#             return None
#     else:
#         warnings.warn(
#             f"no type check for {name} field, type check not implemented for type - {tp}, "
#             f"consider using simplify=True"
#         )
#         return None


# def _check_special_type(tp, name):
#     """checking if the type is a container: ty.List, ty.Dict or ty.Union"""
#     if sys.version_info.minor >= 8:
#         return ty.get_origin(tp), ty.get_args(tp)
#     else:
#         if isinstance(tp, type):  # simple type
#             return None, ()
#         else:
#             if tp._name == "List":
#                 return list, tp.__args__
#             elif tp._name == "Dict":
#                 return dict, tp.__args__
#             elif tp.__origin__ is ty.Union:
#                 return ty.Union, tp.__args__
#             else:
#                 warnings.warn(
#                     f"not type check for {name} field, type check not implemented for type {tp}"
#                 )
#                 return None, ()


def allowed_values_validator(_, attribute, value):
    """checking if the values is in allowed_values"""
    allowed = attribute.metadata["allowed_values"]
    if value is attr.NOTHING or isinstance(value, LazyField):
        pass
    elif value not in allowed:
        raise ValueError(
            f"value of {attribute.name} has to be from {allowed}, but {value} provided"
        )


async def read_stream_and_display(stream, display):
    """
    Read from stream line by line until EOF, display, and capture the lines.

    See Also
    --------
    This `discussion on StackOverflow
    <https://stackoverflow.com/questions/17190221>`__.

    """
    output = []
    while True:
        line = await stream.readline()
        if not line:
            break
        output.append(line)
        if display is not None:
            display(line)  # assume it doesn't block
    return b"".join(output).decode()


async def read_and_display_async(*cmd, hide_display=False, strip=False):
    """
    Capture standard input and output of a process, displaying them as they arrive.

    Works line-by-line.

    """
    # start process
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asp.PIPE, stderr=asp.PIPE
    )

    stdout_display = sys.stdout.buffer.write if not hide_display else None
    stderr_display = sys.stderr.buffer.write if not hide_display else None
    # read child's stdout/stderr concurrently (capture and display)
    try:
        stdout, stderr = await asyncio.gather(
            read_stream_and_display(process.stdout, stdout_display),
            read_stream_and_display(process.stderr, stderr_display),
        )
    except Exception:
        process.kill()
        raise
    finally:
        # wait for the process to exit
        rc = await process.wait()
    if strip:
        return rc, stdout.strip(), stderr
    else:
        return rc, stdout, stderr


def read_and_display(*cmd, strip=False, hide_display=False):
    """Capture a process' standard output."""
    try:
        process = sp.run(cmd, stdout=sp.PIPE, stderr=sp.PIPE)
    except Exception:
        # TODO editing some tracing?
        raise

    if strip:
        return (
            process.returncode,
            process.stdout.decode("utf-8").strip(),
            process.stderr.decode("utf-8"),
        )
    else:
        return (
            process.returncode,
            process.stdout.decode("utf-8"),
            process.stderr.decode("utf-8"),
        )


def execute(cmd, strip=False):
    """
    Run the event loop with coroutine.

    Uses :func:`read_and_display_async` unless a loop is
    already running, in which case :func:`read_and_display`
    is used.

    Parameters
    ----------
    cmd : :obj:`list` or :obj:`tuple`
        The command line to be executed.
    strip : :obj:`bool`
        TODO

    """
    rc, stdout, stderr = read_and_display(*cmd, strip=strip)
    """
    loop = get_open_loop()
    if loop.is_running():
        rc, stdout, stderr = read_and_display(*cmd, strip=strip)
    else:
        rc, stdout, stderr = loop.run_until_complete(
            read_and_display_async(*cmd, strip=strip)
        )
    """
    return rc, stdout, stderr


def create_checksum(name, inputs):
    """
    Generate a checksum name for a given combination of task name and inputs.

    Parameters
    ----------
    name : :obj:`str`
        Task name.
    inputs : :obj:`str`
        String of inputs.

    """
    return "_".join((name, inputs))


def record_error(error_path, error):
    """Write an error file."""

    error_message = str(error)

    resultfile = error_path / "_result.pklz"
    if not resultfile.exists():
        error_message += """\n
    When creating this error file, the results file corresponding
    to the task could not be found."""

    name_checksum = str(error_path.name)
    timeofcrash = strftime("%Y%m%d-%H%M%S")
    try:
        login_name = getpass.getuser()
    except KeyError:
        login_name = f"UID{os.getuid():d}"

    full_error = {
        "time of crash": timeofcrash,
        "login name": login_name,
        "name with checksum": name_checksum,
        "error message": error,
    }

    with (error_path / "_error.pklz").open("wb") as fp:
        cp.dump(full_error, fp)

    return error_path / "_error.pklz"


def get_open_loop():
    """
    Get current event loop.

    If the loop is closed, a new
    loop is created and set as the current event loop.

    Returns
    -------
    loop : :obj:`asyncio.EventLoop`
        The current event loop

    """
    if os.name == "nt":
        loop = asyncio.ProactorEventLoop()  # for subprocess' pipes on Windows
    else:
        try:
            loop = asyncio.get_event_loop()
        # in case RuntimeError: There is no current event loop in thread 'MainThread'
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        else:
            if loop.is_closed():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
    return loop


def hash_function(obj):
    """Generate hash of object."""
    return hash_object(obj).hex()


def hash_value(value, tp=None, metadata=None, precalculated=None):
    """calculating hash or returning values recursively"""
    if metadata is None:
        metadata = {}
    if isinstance(value, (tuple, list, set)):
        return [hash_value(el, tp, metadata, precalculated) for el in value]
    elif isinstance(value, dict):
        dict_hash = {
            k: hash_value(v, tp, metadata, precalculated) for (k, v) in value.items()
        }
        # returning a sorted object
        return [list(el) for el in sorted(dict_hash.items(), key=lambda x: x[0])]
    else:  # not a container
        if (
            (tp is File or "pydra.engine.specs.File" in str(tp))
            and is_existing_file(value)
            and "container_path" not in metadata
        ):
            return hash_file(value, precalculated=precalculated)
        elif (
            (tp is File or "pydra.engine.specs.Directory" in str(tp))
            and is_existing_file(value)
            and "container_path" not in metadata
        ):
            return hash_dir(value, precalculated=precalculated)
        elif type(value).__module__ == "numpy":  # numpy objects
            return [
                hash_value(el, tp, metadata, precalculated)
                for el in ensure_list(value.tolist())
            ]
        else:
            return value


def output_from_inputfields(output_spec, input_spec):
    """
    Collect values from output from input fields.
    If names_only is False, the output_spec is updated,
    if names_only is True only the names are returned

    Parameters
    ----------
    output_spec :
        TODO
    input_spec :
        TODO

    """
    current_output_spec_names = [f.name for f in attr.fields(make_klass(output_spec))]
    new_fields = []
    for fld in attr.fields(make_klass(input_spec)):
        if "output_file_template" in fld.metadata:
            if "output_field_name" in fld.metadata:
                field_name = fld.metadata["output_field_name"]
            else:
                field_name = fld.name
            # not adding if the field already in the output_spec
            if field_name not in current_output_spec_names:
                # TODO: should probably remove some of the keys
                new_fields.append(
                    (field_name, attr.ib(type=File, metadata=fld.metadata))
                )
    output_spec.fields += new_fields
    return output_spec


def get_available_cpus():
    """
    Return the number of CPUs available to the current process or, if that is not
    available, the total number of CPUs on the system.

    Returns
    -------
    n_proc : :obj:`int`
        The number of available CPUs.
    """
    # Will not work on some systems or if psutil is not installed.
    # See https://psutil.readthedocs.io/en/latest/#psutil.Process.cpu_affinity
    try:
        import psutil

        return len(psutil.Process().cpu_affinity())
    except (AttributeError, ImportError, NotImplementedError):
        pass

    # Not available on all systems, including macOS.
    # See https://docs.python.org/3/library/os.html#os.sched_getaffinity
    if hasattr(os, "sched_getaffinity"):
        return len(os.sched_getaffinity(0))

    # Last resort
    return os.cpu_count()


def load_and_run(
    task_pkl, ind=None, rerun=False, submitter=None, plugin=None, **kwargs
):
    """
    loading a task from a pickle file, settings proper input
    and running the task
    """
    try:
        task = load_task(task_pkl=task_pkl, ind=ind)
    except Exception:
        if task_pkl.parent.exists():
            etype, eval, etr = sys.exc_info()
            traceback = format_exception(etype, eval, etr)
            errorfile = record_error(task_pkl.parent, error=traceback)
            result = Result(output=None, runtime=None, errored=True)
            save(task_pkl.parent, result=result)
        raise

    resultfile = task.output_dir / "_result.pklz"
    try:
        task(rerun=rerun, plugin=plugin, submitter=submitter, **kwargs)
    except Exception as excinfo:
        # creating result and error files if missing
        errorfile = task.output_dir / "_error.pklz"
        if not errorfile.exists():  # not sure if this is needed
            etype, eval, etr = sys.exc_info()
            traceback = format_exception(etype, eval, etr)
            errorfile = record_error(task.output_dir, error=traceback)
        if not resultfile.exists():  # not sure if this is needed
            result = Result(output=None, runtime=None, errored=True)
            save(task.output_dir, result=result)
        raise type(excinfo)(
            str(excinfo.with_traceback(None)),
            f" full crash report is here: {errorfile}",
        )
    return resultfile


async def load_and_run_async(task_pkl, ind=None, submitter=None, rerun=False, **kwargs):
    """
    loading a task from a pickle file, settings proper input
    and running the workflow
    """
    task = load_task(task_pkl=task_pkl, ind=ind)
    await task._run(submitter=submitter, rerun=rerun, **kwargs)


def load_task(task_pkl, ind=None):
    """loading a task from a pickle file, settings proper input for the specific ind"""
    if isinstance(task_pkl, str):
        task_pkl = Path(task_pkl)
    task = cp.loads(task_pkl.read_bytes())
    if ind is not None:
        _, inputs_dict = task.get_input_el(ind)
        task.inputs = attr.evolve(task.inputs, **inputs_dict)
        task.state = None
        # resetting uid for task
        task._uid = uuid4().hex
    return task


def position_sort(args):
    """
    Sort objects by position, following Python indexing conventions.

    Ordering is positive positions, lowest to highest, followed by unspecified
    positions (``None``) and negative positions, lowest to highest.

    >>> position_sort([(None, "d"), (-3, "e"), (2, "b"), (-2, "f"), (5, "c"), (1, "a")])
    ['a', 'b', 'c', 'd', 'e', 'f']

    Parameters
    ----------
    args : list of (int/None, object) tuples

    Returns
    -------
    list of objects
    """
    import bisect

    pos, none, neg = [], [], []
    for entry in args:
        position = entry[0]
        if position is None:
            # Take existing order
            none.append(entry[1])
        elif position < 0:
            # Sort negatives while collecting
            bisect.insort(neg, entry)
        else:
            # Sort positives while collecting
            bisect.insort(pos, entry)

    return [arg for _, arg in pos] + none + [arg for _, arg in neg]


def argstr_formatting(argstr, inputs, value_updates=None):
    """formatting argstr that have form {field_name},
    using values from inputs and updating with value_update if provided
    """
    inputs_dict = attr.asdict(inputs, recurse=False)
    # if there is a value that has to be updated (e.g. single value from a list)
    if value_updates:
        inputs_dict.update(value_updates)
    # getting all fields that should be formatted, i.e. {field_name}, ...
    inp_fields = re.findall(r"{\w+}", argstr)
    inp_fields_float = re.findall(r"{\w+:[0-9.]+f}", argstr)
    inp_fields += [re.sub(":[0-9.]+f", "", el) for el in inp_fields_float]
    val_dict = {}
    for fld in inp_fields:
        fld_name = fld[1:-1]  # extracting the name form {field_name}
        fld_value = inputs_dict[fld_name]
        if fld_value is attr.NOTHING:
            # if value is NOTHING, nothing should be added to the command
            val_dict[fld_name] = ""
        else:
            val_dict[fld_name] = fld_value

    # formatting string based on the val_dict
    argstr_formatted = argstr.format(**val_dict)
    # removing extra commas and spaces after removing the field that have NOTHING
    argstr_formatted = (
        argstr_formatted.replace("[ ", "[")
        .replace(" ]", "]")
        .replace("[,", "[")
        .replace(",]", "]")
        .strip()
    )
    return argstr_formatted


class PydraFileLock:
    """Wrapper for filelock's SoftFileLock that makes it work with asyncio."""

    def __init__(self, lockfile):
        self.lockfile = lockfile
        self.timeout = 0.1

    async def __aenter__(self):
        lock = SoftFileLock(self.lockfile)
        acquired_lock = False
        while not acquired_lock:
            try:
                lock.acquire(timeout=0)
                acquired_lock = True
            except Timeout:
                await asyncio.sleep(self.timeout)
                if self.timeout <= 2:
                    self.timeout = self.timeout * 2
        self.lock = lock
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        self.lock.release()
        return None
