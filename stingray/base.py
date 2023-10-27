"""Base classes"""
from __future__ import annotations

from collections.abc import Iterable
import pickle
import warnings
import copy
import os
import logging

import numpy as np
from astropy.table import Table
from astropy.time import Time, TimeDelta
from astropy.units import Quantity

from typing import TYPE_CHECKING, Type, TypeVar, Union

if TYPE_CHECKING:
    from xarray import Dataset
    from pandas import DataFrame
    from astropy.timeseries import TimeSeries
    from astropy.time import TimeDelta
    import numpy.typing as npt

    TTime = Union[Time, TimeDelta, Quantity, npt.ArrayLike]
    Tso = TypeVar("Tso", bound="StingrayObject")

HAS_128 = True
try:
    np.float128
except AttributeError:  # pragma: no cover
    HAS_128 = False


__all__ = [
    "sqsum",
    "convert_table_attrs_to_lowercase",
    "interpret_times",
    "reduce_precision_if_extended",
    "StingrayObject",
    "StingrayTimeseries",
]


def _can_save_longdouble(probe_file: str, fmt: str) -> bool:
    """Check if a given file format can save tables with longdoubles.

    Try to save a table with a longdouble column, and if it doesn't work, catch the exception.
    If the exception is related to longdouble, return False (otherwise just raise it, this
    would mean there are larger problems that need to be solved). In this case, also warn that
    probably part of the data will not be saved.

    If no exception is raised, return True.
    """
    if not HAS_128:  # pragma: no cover
        # There are no known issues with saving longdoubles where numpy.float128 is not defined
        return True

    try:
        Table({"a": np.arange(0, 3, 1.212314).astype(np.float128)}).write(
            probe_file, format=fmt, overwrite=True
        )
        yes_it_can = True
        os.unlink(probe_file)
    except ValueError as e:
        if "float128" not in str(e):  # pragma: no cover
            raise
        warnings.warn(
            f"{fmt} output does not allow saving metadata at maximum precision. "
            "Converting to lower precision"
        )
        yes_it_can = False
    return yes_it_can


def _can_serialize_meta(probe_file: str, fmt: str) -> bool:
    """
    Try to save a table with meta to be serialized, and if it doesn't work, catch the exception.
    If the exception is related to serialization, return False (otherwise just raise it, this
    would mean there are larger problems that need to be solved). In this case, also warn that
    probably part of the data will not be saved.

    If no exception is raised, return True.
    """
    try:
        Table({"a": [3]}).write(probe_file, overwrite=True, format=fmt, serialize_meta=True)

        os.unlink(probe_file)
        yes_it_can = True
    except TypeError as e:
        if "serialize_meta" not in str(e):  # pragma: no cover
            raise
        warnings.warn(
            f"{fmt} output does not serialize the metadata at the moment. "
            "Some attributes will be lost."
        )
        yes_it_can = False
    return yes_it_can


def sqsum(array1, array2):
    """Return the square root of the sum of the squares of two arrays."""
    return np.sqrt(np.add(np.square(array1), np.square(array2)))


def convert_table_attrs_to_lowercase(table: Table) -> Table:
    """Convert the column names of an Astropy Table to lowercase."""
    new_table = Table()
    for col in table.colnames:
        new_table[col.lower()] = table[col]
    for key in table.meta.keys():
        new_table.meta[key.lower()] = table.meta[key]

    return new_table


class StingrayObject(object):
    """This base class defines some general-purpose utilities.

    The main purpose is to have a consistent mechanism for:

    + round-tripping to and from Astropy Tables and other dataframes

    + round-tripping to files in different formats

    The idea is that any object inheriting :class:`StingrayObject` should,
    just by defining an attribute called ``main_array_attr``, be able to perform
    the operations above, with no additional effort.

    ``main_array_attr`` is, e.g. ``time`` for :class:`StingrayTimeseries` and
    :class:`Lightcurve`, ``freq`` for :class:`Crossspectrum`, ``energy`` for
    :class:`VarEnergySpectrum`, and so on. It is the array with wich all other
    attributes are compared: if they are of the same shape, they get saved as
    columns of the table/dataframe, otherwise as metadata.
    """

    not_array_attr: list = []

    def __init__(cls, *args, **kwargs) -> None:
        if not hasattr(cls, "main_array_attr"):
            raise RuntimeError(
                "A StingrayObject needs to have the main_array_attr attribute specified"
            )

    def array_attrs(self) -> list[str]:
        """List the names of the array attributes of the Stingray Object.

        By array attributes, we mean the ones with the same size and shape as
        ``main_array_attr`` (e.g. ``time`` in ``EventList``)
        """

        main_attr = getattr(self, getattr(self, "main_array_attr"))
        if main_attr is None:
            return []

        return [
            attr
            for attr in self.data_attributes()
            if (
                not attr.startswith("_")
                and not attr == self.main_array_attr
                and isinstance(getattr(self, attr), Iterable)
                and attr not in self.not_array_attr
                and not isinstance(getattr(self, attr), str)
                and np.shape(getattr(self, attr))[0] == np.shape(main_attr)[0]
            )
        ]

    def data_attributes(self) -> list[str]:
        """Clean up the list of attributes, only giving out actual data.

        This also includes properties (which usually set internal data arrays, so they would
        duplicate the effort), methods, and attributes that are complicated to serialize such
        as other ``StingrayObject``, or arrays of objects.
        """
        return [
            attr
            for attr in dir(self)
            if (
                not attr.startswith("__")
                and attr not in ["main_array_attr", "not_array_attr"]
                and not isinstance(getattr(self.__class__, attr, None), property)
                and not callable(value := getattr(self, attr))
                and not isinstance(value, StingrayObject)
                and not np.asarray(value).dtype == "O"
            )
        ]

    def internal_array_attrs(self) -> list[str]:
        """List the names of the array attributes of the Stingray Object.

        By array attributes, we mean the ones with the same size and shape as
        ``main_array_attr`` (e.g. ``time`` in ``EventList``)
        """

        main_attr = getattr(self, "main_array_attr")
        main_attr_value = getattr(self, main_attr)
        if main_attr_value is None:
            return []

        all_attrs = []
        for attr in self.data_attributes():
            if (
                not attr == "_" + self.main_array_attr  # e.g. _time in lightcurve
                and attr not in ["_" + a for a in self.not_array_attr]
                and not np.isscalar(value := getattr(self, attr))
                and value is not None
                and not np.size(value) == 0
                and attr.startswith("_")
                and np.shape(value)[0] == np.shape(main_attr_value)[0]
            ):
                all_attrs.append(attr)

        return all_attrs

    def meta_attrs(self) -> list[str]:
        """List the names of the meta attributes of the Stingray Object.

        By array attributes, we mean the ones with a different size and shape
        than ``main_array_attr`` (e.g. ``time`` in ``EventList``)
        """
        array_attrs = self.array_attrs() + [self.main_array_attr] + self.internal_array_attrs()

        all_meta_attrs = [
            attr
            for attr in self.data_attributes()
            if (attr not in array_attrs and not attr.startswith("_"))
        ]
        if self.not_array_attr is not None and len(self.not_array_attr) >= 1:
            all_meta_attrs += self.not_array_attr
        return all_meta_attrs

    def __eq__(self, other_ts):
        """Compare two :class:`StingrayObject` instances with ``==``.

        All attributes (internal, array, meta) are compared.
        """

        if not isinstance(other_ts, type(self)):
            raise ValueError(f"{type(self)} can only be compared with a {type(self)} Object")

        self_arr_attrs = self.array_attrs()
        other_arr_attrs = other_ts.array_attrs()

        if not set(self_arr_attrs) == set(other_arr_attrs):
            return False

        self_meta_attrs = self.meta_attrs()
        other_meta_attrs = other_ts.meta_attrs()

        if not set(self_meta_attrs) == set(other_meta_attrs):
            return False

        for attr in self.meta_attrs():
            # They are either both scalar or arrays
            if np.isscalar(getattr(self, attr)) != np.isscalar(getattr(other_ts, attr)):
                return False

            if np.isscalar(getattr(self, attr)):
                if not getattr(self, attr, None) == getattr(other_ts, attr, None):
                    return False
            else:
                if not np.array_equal(getattr(self, attr, None), getattr(other_ts, attr, None)):
                    return False

        for attr in self.array_attrs():
            if not np.array_equal(getattr(self, attr), getattr(other_ts, attr)):
                return False

        for attr in self.internal_array_attrs():
            if not np.array_equal(getattr(self, attr), getattr(other_ts, attr)):
                return False

        return True

    def _default_operated_attrs(self):
        operated_attrs = [attr for attr in self.array_attrs() if not attr.endswith("_err")]
        return operated_attrs

    def _default_error_attrs(self):
        return [attr for attr in self.array_attrs() if attr.endswith("_err")]

    def get_meta_dict(self) -> dict:
        """Give a dictionary with all non-None meta attrs of the object."""
        meta_attrs = self.meta_attrs()
        meta_dict = {}
        for key in meta_attrs:
            val = getattr(self, key)
            if val is not None:
                meta_dict[key] = val
        return meta_dict

    def to_astropy_table(self, no_longdouble=False) -> Table:
        """Create an Astropy Table from a ``StingrayObject``

        Array attributes (e.g. ``time``, ``pi``, ``energy``, etc. for
        ``EventList``) are converted into columns, while meta attributes
        (``mjdref``, ``gti``, etc.) are saved into the ``meta`` dictionary.
        """
        data = {}
        array_attrs = self.array_attrs() + [self.main_array_attr] + self.internal_array_attrs()

        for attr in array_attrs:
            vals = np.asarray(getattr(self, attr))
            if no_longdouble:
                vals = reduce_precision_if_extended(vals)
            data[attr] = vals

        ts = Table(data)
        meta_dict = self.get_meta_dict()
        for attr in meta_dict.keys():
            if no_longdouble:
                meta_dict[attr] = reduce_precision_if_extended(meta_dict[attr])

        ts.meta.update(meta_dict)

        return ts

    @classmethod
    def from_astropy_table(cls: Type[Tso], ts: Table) -> Tso:
        """Create a Stingray Object object from data in an Astropy Table.

        The table MUST contain at least a column named like the
        ``main_array_attr``.
        The rest of columns will form the array attributes of the
        new object, while the attributes in ds.attrs will
        form the new meta attributes of the object.

        """
        cls = cls()

        if len(ts) == 0:
            # return an empty object
            return cls

        array_attrs = ts.colnames

        # Set the main attribute first
        for attr in array_attrs:
            if attr.lower() == cls.main_array_attr:  # type: ignore
                mainarray = np.array(ts[attr])  # type: ignore
                setattr(cls, cls.main_array_attr, mainarray)  # type: ignore
                break

        for attr in array_attrs:
            if attr.lower() == cls.main_array_attr:  # type: ignore
                continue
            setattr(cls, attr.lower(), np.array(ts[attr]))

        for key, val in ts.meta.items():
            setattr(cls, key.lower(), val)

        return cls

    def to_xarray(self) -> Dataset:
        """Create an ``xarray`` Dataset from a `StingrayObject`.

        Array attributes (e.g. ``time``, ``pi``, ``energy``, etc. for
        ``EventList``) are converted into columns, while meta attributes
        (``mjdref``, ``gti``, etc.) are saved into the ``ds.attrs`` dictionary.
        """
        from xarray import Dataset

        data = {}
        array_attrs = self.array_attrs() + [self.main_array_attr] + self.internal_array_attrs()

        for attr in array_attrs:
            new_data = np.asarray(getattr(self, attr))
            ndim = len(np.shape(new_data))
            if ndim > 1:
                new_data = ([attr + f"_dim{i}" for i in range(ndim)], new_data)
            data[attr] = new_data

        ts = Dataset(data)

        ts.attrs.update(self.get_meta_dict())

        return ts

    @classmethod
    def from_xarray(cls: Type[Tso], ts: Dataset) -> Tso:
        """Create a `StingrayObject` from data in an xarray Dataset.

        The dataset MUST contain at least a column named like the
        ``main_array_attr``.
        The rest of columns will form the array attributes of the
        new object, while the attributes in ds.attrs will
        form the new meta attributes of the object.
        """
        cls = cls()

        if len(ts[cls.main_array_attr]) == 0:  # type: ignore
            # return an empty object
            return cls

        # Set the main attribute first
        mainarray = np.array(ts[cls.main_array_attr])  # type: ignore
        setattr(cls, cls.main_array_attr, mainarray)  # type: ignore

        all_array_attrs = []
        for array_attrs in [ts.coords, ts.data_vars]:
            for attr in array_attrs:
                all_array_attrs.append(attr)
                if attr == cls.main_array_attr:  # type: ignore
                    continue
                setattr(cls, attr, np.array(ts[attr]))

        for key, val in ts.attrs.items():
            if key not in all_array_attrs:
                setattr(cls, key, val)

        return cls

    def to_pandas(self) -> DataFrame:
        """Create a pandas ``DataFrame`` from a :class:`StingrayObject`.

        Array attributes (e.g. ``time``, ``pi``, ``energy``, etc. for
        ``EventList``) are converted into columns, while meta attributes
        (``mjdref``, ``gti``, etc.) are saved into the ``ds.attrs`` dictionary.

        Since pandas does not support n-D data, multi-dimensional arrays are
        converted into columns before the conversion, with names ``<colname>_dimN_M_K`` etc.

        See documentation of `make_nd_into_arrays` for details.

        """
        from pandas import DataFrame
        from .utils import make_nd_into_arrays

        data = {}
        array_attrs = self.array_attrs() + [self.main_array_attr] + self.internal_array_attrs()

        for attr in array_attrs:
            values = np.asarray(getattr(self, attr))
            ndim = len(np.shape(values))
            if ndim > 1:
                local_data = make_nd_into_arrays(values, attr)
            else:
                local_data = {attr: values}
            data.update(local_data)

        ts = DataFrame(data)

        ts.attrs.update(self.get_meta_dict())

        return ts

    @classmethod
    def from_pandas(cls: Type[Tso], ts: DataFrame) -> Tso:
        """Create an `StingrayObject` object from data in a pandas DataFrame.

        The dataframe MUST contain at least a column named like the
        ``main_array_attr``.
        The rest of columns will form the array attributes of the
        new object, while the attributes in ds.attrs will
        form the new meta attributes of the object.

        Since pandas does not support n-D data, multi-dimensional arrays can be
        specified as ``<colname>_dimN_M_K`` etc.

        See documentation of `make_1d_arrays_into_nd` for details.

        """
        import re
        from .utils import make_1d_arrays_into_nd

        cls = cls()

        if len(ts) == 0:
            # return an empty object
            return cls

        array_attrs = ts.columns

        # Set the main attribute first
        mainarray = np.array(ts[cls.main_array_attr])  # type: ignore
        setattr(cls, cls.main_array_attr, mainarray)  # type: ignore

        nd_attrs = []
        for attr in array_attrs:
            if attr == cls.main_array_attr:  # type: ignore
                continue
            if "_dim" in attr:
                nd_attrs.append(re.sub("_dim[0-9].*", "", attr))
            else:
                setattr(cls, attr, np.array(ts[attr]))

        for attr in list(set(nd_attrs)):
            setattr(cls, attr, make_1d_arrays_into_nd(ts, attr))

        for key, val in ts.attrs.items():
            if key not in array_attrs:
                setattr(cls, key, val)

        return cls

    @classmethod
    def read(cls: Type[Tso], filename: str, fmt: str = None) -> Tso:
        r"""Generic reader for :class`StingrayObject`

        Currently supported formats are

        * pickle (not recommended for long-term storage)
        * any other formats compatible with the writers in
          :class:`astropy.table.Table` (ascii.ecsv, hdf5, etc.)

        Files that need the :class:`astropy.table.Table` interface MUST contain
        at least a column named like the ``main_array_attr``.
        The default ascii format is enhanced CSV (ECSV). Data formats
        supporting the serialization of metadata (such as ECSV and HDF5) can
        contain all attributes such as ``mission``, ``gti``, etc with
        no significant loss of information. Other file formats might lose part
        of the metadata, so must be used with care.

        ..note::

            Complex values can be dealt with out-of-the-box in some formats
            like HDF5 or FITS, not in others (e.g. all ASCII formats).
            With these formats, and in any case when fmt is ``None``, complex
            values should be stored as two columns of real numbers, whose names
            are of the format <variablename>.real and <variablename>.imag

        Parameters
        ----------
        filename: str
            Path and file name for the file to be read.

        fmt: str
            Available options are 'pickle', 'hea', and any `Table`-supported
            format such as 'hdf5', 'ascii.ecsv', etc.

        Returns
        -------
        obj: :class:`StingrayObject` object
            The object reconstructed from file
        """

        if fmt is None:
            pass
        elif fmt.lower() == "pickle":
            with open(filename, "rb") as fobj:
                return pickle.load(fobj)
        elif fmt.lower() == "ascii":
            fmt = "ascii.ecsv"

        ts = convert_table_attrs_to_lowercase(Table.read(filename, format=fmt))

        # For specific formats, and in any case when the format is not
        # specified, make sure that complex values are treated correctly.
        if fmt is None or "ascii" in fmt:
            for col in ts.colnames:
                if not ((is_real := col.endswith(".real")) or (is_imag := col.endswith(".imag"))):
                    continue

                new_value = ts[col]

                if is_imag:
                    new_value = new_value * 1j

                # Make sure it's complex, even if we find the real part first
                new_value = new_value + 0.0j

                col_strip = col.replace(".real", "").replace(".imag", "")

                if col_strip not in ts.colnames:
                    # If the column without ".real" or ".imag" doesn't exist,
                    # define it, and make sure it's complex-valued
                    ts[col_strip] = new_value
                else:
                    # If it does exist, sum the new value to it.
                    ts[col_strip] += new_value

                ts.remove_column(col)

        return cls.from_astropy_table(ts)

    def write(self, filename: str, fmt: str = None) -> None:
        """Generic writer for :class`StingrayObject`

        Currently supported formats are

        * pickle (not recommended for long-term storage)
        * any other formats compatible with the writers in
          :class:`astropy.table.Table` (ascii.ecsv, hdf5, etc.)

        ..note::

            Complex values can be dealt with out-of-the-box in some formats
            like HDF5 or FITS, not in others (e.g. all ASCII formats).
            With these formats, and in any case when fmt is ``None``, complex
            values will be stored as two columns of real numbers, whose names
            are of the format <variablename>.real and <variablename>.imag

        Parameters
        ----------
        filename: str
            Name and path of the file to save the object list to.

        fmt: str
            The file format to store the data in.
            Available options are ``pickle``, ``hdf5``, ``ascii``, ``fits``
        """
        if fmt is None:
            pass
        elif fmt.lower() == "pickle":
            with open(filename, "wb") as fobj:
                pickle.dump(self, fobj)
            return
        elif fmt.lower() == "ascii":
            fmt = "ascii.ecsv"

        probe_file = "probe.bu.bu." + filename[-7:]

        CAN_SAVE_LONGD = _can_save_longdouble(probe_file, fmt)
        CAN_SERIALIZE_META = _can_serialize_meta(probe_file, fmt)

        to_be_saved = self

        ts = to_be_saved.to_astropy_table(no_longdouble=not CAN_SAVE_LONGD)

        if fmt is None or "ascii" in fmt:
            for col in ts.colnames:
                if np.iscomplex(ts[col].flatten()[0]):
                    ts[f"{col}.real"] = ts[col].real
                    ts[f"{col}.imag"] = ts[col].imag
                    ts.remove_column(col)

        if CAN_SERIALIZE_META:
            ts.write(filename, format=fmt, overwrite=True, serialize_meta=True)
        else:
            ts.write(filename, format=fmt, overwrite=True)

    def apply_mask(self, mask: npt.ArrayLike, inplace: bool = False, filtered_attrs: list = None):
        """Apply a mask to all array attributes of the time series

        Parameters
        ----------
        mask : array of ``bool``
            The mask. Has to be of the same length as ``self.time``

        Other parameters
        ----------------
        inplace : bool
            If True, overwrite the current time series. Otherwise, return a new one.
        filtered_attrs : list of str or None
            Array attributes to be filtered. Defaults to all array attributes if ``None``.
            The other array attributes will be set to ``None``. The main array attr is always
            included.

        """
        all_attrs = self.internal_array_attrs() + self.array_attrs()
        if filtered_attrs is None:
            filtered_attrs = all_attrs

        if inplace:
            new_ts = self
        else:
            new_ts = type(self)()
            for attr in self.meta_attrs():
                setattr(new_ts, attr, copy.deepcopy(getattr(self, attr)))

        # If the main array attr is managed through an internal attr
        # (e.g. lightcurve), set the internal attr instead.
        if hasattr(self, "_" + self.main_array_attr):
            setattr(
                new_ts,
                "_" + self.main_array_attr,
                copy.deepcopy(np.asarray(getattr(self, self.main_array_attr))[mask]),
            )
        else:
            setattr(
                new_ts,
                self.main_array_attr,
                copy.deepcopy(np.asarray(getattr(self, self.main_array_attr))[mask]),
            )

        for attr in all_attrs:
            if attr not in filtered_attrs:
                # Eliminate all unfiltered attributes
                setattr(new_ts, attr, None)
            else:
                setattr(new_ts, attr, copy.deepcopy(np.asarray(getattr(self, attr))[mask]))
        return new_ts

    def _operation_with_other_obj(
        self,
        other,
        operation,
        operated_attrs=None,
        error_attrs=None,
        error_operation=None,
        inplace=False,
    ):
        """
        Helper method to codify an operation of one time series with another (e.g. add, subtract).
        Takes into account the GTIs, and returns a new :class:`StingrayTimeseries` object.

        Parameters
        ----------
        other : :class:`StingrayTimeseries` object
            A second time series object

        operation : function
            An operation between the :class:`StingrayTimeseries` object calling this method, and
            ``other``, operating on all the specified array attributes.

        Other parameters
        ----------------
        operated_attrs : list of str or None
            Array attributes to be operated on. Defaults to all array attributes not ending in
            ``_err``.
            The other array attributes will be discarded from the time series to avoid
            inconsistencies.

        error_attrs : list of str or None
            Array attributes to be operated on with ``error_operation``. Defaults to all array
            attributes ending with ``_err``.

        error_operation : function
            The function used for error propagation. Defaults to the sum of squares.

        Returns
        -------
        ts_new : StingrayTimeseries object
            The new time series calculated in ``operation``
        """

        if operated_attrs is None:
            operated_attrs = self._default_operated_attrs()

        if error_attrs is None:
            error_attrs = self._default_error_attrs()

        if not isinstance(other, type(self)):
            raise TypeError(
                f"{type(self)} objects can only be operated with other {type(self)} objects."
            )

        this_time = getattr(self, self.main_array_attr)
        # ValueError is raised by Numpy while asserting np.equal over arrays
        # with different dimensions.
        try:
            assert np.array_equal(this_time, getattr(other, self.main_array_attr))
        except (ValueError, AssertionError):
            raise ValueError(
                f"The values of {self.main_array_attr} are different in the two {type(self)} "
                "objects."
            )

        if inplace:
            ts_new = self
        else:
            ts_new = type(self)()
        setattr(ts_new, self.main_array_attr, this_time)
        for attr in self.meta_attrs():
            setattr(ts_new, attr, copy.deepcopy(getattr(self, attr)))

        for attr in operated_attrs:
            setattr(
                ts_new,
                attr,
                operation(getattr(self, attr), getattr(other, attr)),
            )

        for attr in error_attrs:
            setattr(
                ts_new,
                attr,
                error_operation(getattr(self, attr), getattr(other, attr)),
            )

        return ts_new

    def add(
        self, other, operated_attrs=None, error_attrs=None, error_operation=sqsum, inplace=False
    ):
        """Add the array values of two time series element by element, assuming the ``time`` arrays
        of the time series match exactly.

        All array attrs ending with ``_err`` are treated as error bars and propagated with the
        sum of squares.

        GTIs are crossed, so that only common intervals are saved.

        Parameters
        ----------
        other : :class:`StingrayTimeseries` object
            A second time series object

        Other parameters
        ----------------
        operated_attrs : list of str or None
            Array attributes to be operated on. Defaults to all array attributes not ending in
            ``_err``.
            The other array attributes will be discarded from the time series to avoid
            inconsistencies.
        error_attrs : list of str or None
            Array attributes to be operated on with ``error_operation``. Defaults to all array
            attributes ending with ``_err``.
        error_operation : function
            Function to be called to propagate the errors
        inplace : bool
            If True, overwrite the current time series. Otherwise, return a new one.
        """
        return self._operation_with_other_obj(
            other,
            np.add,
            operated_attrs=operated_attrs,
            error_attrs=error_attrs,
            error_operation=error_operation,
            inplace=inplace,
        )

    def __add__(self, other):
        """Operation that gets called with the ``+`` operator.

        Add the array values of two time series element by element, assuming the ``time`` arrays
        of the time series match exactly.

        All array attrs ending with ``_err`` are treated as error bars and propagated with the
        sum of squares.

        GTIs are crossed, so that only common intervals are saved.
        """

        return self._operation_with_other_obj(
            other,
            np.add,
            error_operation=sqsum,
        )

    def __iadd__(self, other):
        """Operation that gets called with the ``+=`` operator.

        Add the array values of two time series element by element, assuming the ``time`` arrays
        of the time series match exactly.

        All array attrs ending with ``_err`` are treated as error bars and propagated with the
        sum of squares.

        GTIs are crossed, so that only common intervals are saved.
        """

        return self._operation_with_other_obj(
            other,
            np.add,
            error_operation=sqsum,
            inplace=True,
        )

    def sub(
        self, other, operated_attrs=None, error_attrs=None, error_operation=sqsum, inplace=False
    ):
        """
        Subtract *all the array attrs* of one time series from the ones of another
        time series element by element, assuming the ``time`` arrays of the time series
        match exactly.

        All array attrs ending with ``_err`` are treated as error bars and propagated with the
        sum of squares.

        GTIs are crossed, so that only common intervals are saved.

        Parameters
        ----------
        other : :class:`StingrayTimeseries` object
            A second time series object

        Other parameters
        ----------------
        operated_attrs : list of str or None
            Array attributes to be operated on. Defaults to all array attributes not ending in
            ``_err``.
            The other array attributes will be discarded from the time series to avoid
            inconsistencies.
        error_attrs : list of str or None
            Array attributes to be operated on with ``error_operation``. Defaults to all array
            attributes ending with ``_err``.
        error_operation : function
            Function to be called to propagate the errors
        inplace : bool
            If True, overwrite the current time series. Otherwise, return a new one.
        """
        return self._operation_with_other_obj(
            other,
            np.subtract,
            operated_attrs=operated_attrs,
            error_attrs=error_attrs,
            error_operation=error_operation,
            inplace=inplace,
        )

    def __sub__(self, other):
        """Operation that gets called with the ``-`` operator.

        Subtract *all the array attrs* of one time series from the ones of another
        time series element by element, assuming the ``time`` arrays of the time series
        match exactly.

        All array attrs ending with ``_err`` are treated as error bars and propagated with the
        sum of squares.

        GTIs are crossed, so that only common intervals are saved.
        """

        return self._operation_with_other_obj(
            other,
            np.subtract,
            error_operation=sqsum,
        )

    def __isub__(self, other):
        """Operation that gets called with the ``-=`` operator.

        Subtract *all the array attrs* of one time series from the ones of another
        time series element by element, assuming the ``time`` arrays of the time series
        match exactly.

        All array attrs ending with ``_err`` are treated as error bars and propagated with the
        sum of squares.

        GTIs are crossed, so that only common intervals are saved.
        """

        return self._operation_with_other_obj(
            other,
            np.subtract,
            error_operation=sqsum,
            inplace=True,
        )

    def __neg__(self):
        """
        Implement the behavior of negation of the array attributes of a time series object.
        Error attrs are left alone.

        The negation operator ``-`` is supposed to invert the sign of the count
        values of a time series object.

        """

        ts_new = copy.deepcopy(self)
        for attr in self._default_operated_attrs():
            setattr(ts_new, attr, -np.asarray(getattr(self, attr)))

        return ts_new

    def __len__(self):
        """
        Return the number of time bins of a time series.

        This method implements overrides the ``len`` function for a :class:`StingrayTimeseries`
        object and returns the length of the array attributes (using the main array attribute
        as probe).
        """
        return np.size(getattr(self, self.main_array_attr))

    def __getitem__(self, index):
        """
        Return the corresponding count value at the index or a new :class:`StingrayTimeseries`
        object upon slicing.

        This method adds functionality to retrieve the count value at
        a particular index. This also can be used for slicing and generating
        a new :class:`StingrayTimeseries` object. GTIs are recalculated based on the new light
        curve segment

        If the slice object is of kind ``start:stop:step``, GTIs are also sliced,
        and rewritten as ``zip(time - self.dt /2, time + self.dt / 2)``

        Parameters
        ----------
        index : int or slice instance
            Index value of the time array or a slice object.

        """
        from .utils import assign_value_if_none

        if isinstance(index, (int, np.integer)):
            start = index
            stop = index + 1
            step = 1
        elif isinstance(index, slice):
            start = assign_value_if_none(index.start, 0)
            stop = assign_value_if_none(index.stop, len(self))
            step = assign_value_if_none(index.step, 1)
        else:
            raise IndexError("The index must be either an integer or a slice " "object !")

        new_ts = type(self)()
        for attr in self.meta_attrs():
            setattr(new_ts, attr, copy.deepcopy(getattr(self, attr)))

        for attr in self.array_attrs() + [self.main_array_attr]:
            setattr(new_ts, attr, getattr(self, attr)[start:stop:step])

        return new_ts


class StingrayTimeseries(StingrayObject):
    """Basic class for time series data.

    This can be events, binned light curves, unevenly sampled light curves, etc. The only
    requirement is that the data are associated with a time measurement.
    We make a distinction between the *array* attributes, which have the same length of the
    ``time`` array, and the *meta* attributes, which can be scalars or arrays of different
    size. The array attributes can be multidimensional (e.g. a spectrum for each time bin),
    but their first dimension (``array.shape[0]``) must have same length of the ``time`` array.

    Array attributes are singled out automatically depending on their shape. All filtering
    operations (e.g. ``apply_gtis``, ``rebin``, etc.) are applied to array attributes only.
    For this reason, it is advisable to specify whether a given attribute should *not* be
    considered as an array attribute by adding it to the ``not_array_attr`` list.

    Parameters
    ----------
    time: iterable
        A list or array of time stamps

    Other Parameters
    ----------------
    array_attrs : dict
        Array attributes to be set (e.g. ``{"flux": flux_array, "flux_err": flux_err_array}``).
        In principle, they could be specified as simple keyword arguments. But this way, we
        will run a check on the length of the arrays, and raise an error if they are not of a
        shape compatible with the ``time`` array.

    dt: float
        The time resolution of the time series. Can be a scalar or an array attribute (useful
        for non-uniformly sampled data or events from different instruments)

    mjdref : float
        The MJD used as a reference for the time array.

    gtis: ``[[gti0_0, gti0_1], [gti1_0, gti1_1], ...]``
        Good Time Intervals

    high_precision : bool
        Change the precision of self.time to float128. Useful while dealing with fast pulsars.

    timeref : str
        The time reference, as recorded in the FITS file (e.g. SOLARSYSTEM)

    timesys : str
        The time system, as recorded in the FITS file (e.g. TDB)

    ephem : str
        The JPL ephemeris used to barycenter the data, if any (e.g. DE430)

    **other_kw :
        Used internally. Any other keyword arguments will be set as attributes of the object.

    Attributes
    ----------
    time: numpy.ndarray
        The array of time stamps, in seconds from the reference
        MJD defined in ``mjdref``

    not_array_attr: list
        List of attributes that are never to be considered as array attributes. For example, GTIs
        are not array attributes.

    ncounts: int
        The number of data points in the time series

    dt: float
        The time resolution of the measurements. Can be a scalar or an array attribute (useful
        for non-uniformly sampled data or events from different instruments)

    mjdref : float
        The MJD used as a reference for the time array.

    gtis: ``[[gti0_0, gti0_1], [gti1_0, gti1_1], ...]``
        Good Time Intervals

    high_precision : bool
        Change the precision of self.time to float128. Useful while dealing with fast pulsars.

    """

    main_array_attr: str = "time"
    not_array_attr: list = ["gti"]
    _time: TTime = None
    high_precision: bool = False
    mjdref: TTime = 0.0
    dt: float = 0.0

    def __init__(
        self,
        time: TTime = None,
        array_attrs: dict = {},
        mjdref: TTime = 0,
        notes: str = "",
        gti: npt.ArrayLike = None,
        high_precision: bool = False,
        ephem: str = None,
        timeref: str = None,
        timesys: str = None,
        **other_kw,
    ):
        StingrayObject.__init__(self)

        self.notes = notes
        self.mjdref = mjdref
        self.gti = gti
        self.ephem = ephem
        self.timeref = timeref
        self.timesys = timesys
        self._mask = None
        self.high_precision = high_precision
        self.dt = other_kw.pop("dt", 0)

        self._set_times(time, high_precision=high_precision)
        for kw in other_kw:
            setattr(self, kw, other_kw[kw])
        for kw in array_attrs:
            new_arr = np.asarray(array_attrs[kw])
            if self.time.shape[0] != new_arr.shape[0]:
                raise ValueError(f"Lengths of time and {kw} must be equal.")
            setattr(self, kw, new_arr)

        # if gti is None and self.time is not None and np.size(self.time) > 0:
        #     self.gti = np.asarray([[self.time[0] - 0.5 * self.dt, self.time[-1] + 0.5 * self.dt]])

    def _set_times(self, time, high_precision=False):
        if time is None or np.size(time) == 0:
            self._time = None
            return
        time, _ = interpret_times(time, self.mjdref)
        if not high_precision:
            self._time = np.asarray(time)
        else:
            self._time = np.asarray(time, dtype=np.longdouble)

    def _check_value_size(self, value, attr_name, compare_to_attr):
        """Check if the size of a value is compatible with the size of another attribute.

        Different cases are possible:

        - If the value is None, we return None
        - If the value is a scalar, we fail
        - If the value is an array, we check if it has the correct shape by comparing it with
          another attribute. In the special case where the attribute is the same, if it is None
          we assign the new value. Otherwise, the first dimension of the value and the current
          value of the attribute being compared with has to be the same.

        Parameters
        ----------
        value : array-like or None
            The value to check.
        attr_name : str
            The name of the attribute being checked.
        compare_to_attr : str
            The name of the attribute to compare with.
        """
        if value is None:
            return None
        value = np.asarray(value)
        if len(value.shape) < 1:
            raise ValueError(f"{attr_name} array must be at least 1D")
        # If the attribute we compare it with is the same and it is currently None, we assign it
        # This can happen, e.g. with the time array.
        compare_with = getattr(self, compare_to_attr, None)
        if attr_name == compare_to_attr and compare_with is None:
            return value

        # In the special case where the current value of the attribute being compared
        # is None, this also has to fail.
        if compare_with is None:
            raise ValueError(
                f"Can only assign new {attr_name} if the {compare_to_attr} array is not None"
            )
        if value.shape[0] != compare_with.shape[0]:
            raise ValueError(
                f"Can only assign new {attr_name} of the same shape as the {compare_to_attr} array"
            )
        return value

    @property
    def time(self):
        return self._time

    @time.setter
    def time(self, value):
        value = self._check_value_size(value, "time", "time")
        if value is None:
            for attr in self.internal_array_attrs() + self.array_attrs():
                setattr(self, attr, None)
        self._set_times(value, high_precision=self.high_precision)

    @property
    def gti(self):
        if self._gti is None and self._time is not None:
            if isinstance(self.dt, Iterable):
                dt0 = self.dt[0]
                dt1 = self.dt[-1]
            else:
                dt0 = dt1 = self.dt
            self._gti = np.asarray([[self._time[0] - dt0 / 2, self._time[-1] + dt1 / 2]])
        return self._gti

    @gti.setter
    def gti(self, value):
        if value is None:
            self._gti = None
            return
        value = np.asarray(value)
        self._gti = value
        self._mask = None

    @property
    def mask(self):
        from .gti import create_gti_mask

        if self._mask is None:
            self._mask = create_gti_mask(self.time, self.gti, dt=self.dt)
        return self._mask

    @property
    def n(self):
        if getattr(self, self.main_array_attr, None) is None:
            return 0
        return np.shape(np.asarray(getattr(self, self.main_array_attr)))[0]

    def __eq__(self, other_ts):
        return super().__eq__(other_ts)

    def apply_gtis(self, new_gti=None, inplace: bool = True):
        """
        Apply GTIs to a time series. Filters the ``time``, ``counts``,
        ``countrate``, ``counts_err`` and ``countrate_err`` arrays for all bins
        that fall into Good Time Intervals and recalculates mean countrate
        and the number of bins.

        If the data already have

        Parameters
        ----------
        inplace : bool
            If True, overwrite the current time series. Otherwise, return a new one.

        """
        # I import here to avoid the risk of circular imports
        from .gti import check_gtis, create_gti_mask

        if new_gti is None:
            new_gti = self.gti

        check_gtis(new_gti)

        # This will automatically be recreated from GTIs once I set it to None
        good = create_gti_mask(self.time, new_gti, dt=self.dt)
        newts = self.apply_mask(good, inplace=inplace)
        # Important, otherwise addition/subtraction ops will go into an infinite loop
        if inplace:
            newts.gti = new_gti
        return newts

    def split_by_gti(self, gti=None, min_points=2):
        """
        Split the current :class:`StingrayTimeseries` object into a list of
        :class:`StingrayTimeseries` objects, one for each continuous GTI segment
        as defined in the ``gti`` attribute.

        Parameters
        ----------
        min_points : int, default 1
            The minimum number of data points in each time series. Light
            curves with fewer data points will be ignored.

        Returns
        -------
        list_of_tss : list
            A list of :class:`StingrayTimeseries` objects, one for each GTI segment
        """
        from .gti import gti_border_bins, create_gti_mask

        if gti is None:
            gti = self.gti

        list_of_tss = []

        start_bins, stop_bins = gti_border_bins(gti, self.time, self.dt)
        for i in range(len(start_bins)):
            start = start_bins[i]
            stop = stop_bins[i]

            if (stop - start) < min_points:
                continue

            new_gti = np.array([gti[i]])
            mask = create_gti_mask(self.time, new_gti)

            # Note: GTIs are consistent with default in this case!
            new_ts = self.apply_mask(mask)
            new_ts.gti = new_gti

            list_of_tss.append(new_ts)

        return list_of_tss

    def to_astropy_timeseries(self) -> TimeSeries:
        """Save the ``StingrayTimeseries`` to an ``Astropy`` timeseries.

        Array attributes (time, pi, energy, etc.) are converted
        into columns, while meta attributes (mjdref, gti, etc.)
        are saved into the ``meta`` dictionary.

        Returns
        -------
        ts : `astropy.timeseries.TimeSeries`
            A ``TimeSeries`` object with the array attributes as columns,
            and the meta attributes in the `meta` dictionary
        """
        from astropy.timeseries import TimeSeries
        from astropy.time import TimeDelta
        from astropy import units as u

        data = {}
        array_attrs = self.array_attrs()

        for attr in array_attrs:
            if attr == "time":
                continue
            data[attr] = np.asarray(getattr(self, attr))

        if data == {}:
            data = None

        if self.time is not None and np.size(self.time) > 0:  # type: ignore
            times = TimeDelta(self.time * u.s)  # type: ignore
            ts = TimeSeries(data=data, time=times)
        else:
            ts = TimeSeries()

        ts.meta.update(self.get_meta_dict())

        return ts

    @classmethod
    def from_astropy_timeseries(cls, ts: TimeSeries) -> StingrayTimeseries:
        """Create a `StingrayTimeseries` from data in an Astropy TimeSeries

        The timeseries has to define at least a column called time,
        the rest of columns will form the array attributes of the
        new time series, while the attributes in table.meta will
        form the new meta attributes of the time series.

        Parameters
        ----------
        ts : `astropy.timeseries.TimeSeries`
            A ``TimeSeries`` object with the array attributes as columns,
            and the meta attributes in the `meta` dictionary

        Returns
        -------
        ts : `StingrayTimeseries`
            Timeseries object
        """

        time = ts["time"]
        mjdref = None
        if "mjdref" in ts.meta:
            mjdref = ts.meta["mjdref"]

        new_cls = cls()
        time, mjdref = interpret_times(time, mjdref)
        new_cls.time = np.asarray(time)  # type: ignore

        array_attrs = ts.colnames
        for key, val in ts.meta.items():
            setattr(new_cls, key, val)

        for attr in array_attrs:
            if attr == "time":
                continue
            setattr(new_cls, attr, np.asarray(ts[attr]))

        return new_cls

    def change_mjdref(self, new_mjdref: float, inplace=False) -> StingrayTimeseries:
        """Change the MJD reference time (MJDREF) of the time series

        The times of the time series will be shifted in order to be referred to
        this new MJDREF

        Parameters
        ----------
        new_mjdref : float
            New MJDREF

        Other parameters
        ----------------
        inplace : bool
            If True, overwrite the current time series. Otherwise, return a new one.

        Returns
        -------
        new_ts : :class:`StingrayTimeseries` object
            The new time series, shifted by MJDREF
        """
        time_shift = (self.mjdref - new_mjdref) * 86400  # type: ignore

        ts = self.shift(time_shift, inplace=inplace)
        ts.mjdref = new_mjdref  # type: ignore
        return ts

    def shift(self, time_shift: float, inplace=False) -> StingrayTimeseries:
        """Shift the time and the GTIs by the same amount

        Parameters
        ----------
        time_shift: float
            The time interval by which the time series will be shifted (in
            the same units as the time array in :class:`StingrayTimeseries`

        Other parameters
        ----------------
        inplace : bool
            If True, overwrite the current time series. Otherwise, return a new one.

        Returns
        -------
        ts : ``StingrayTimeseries`` object
            The new time series shifted by ``time_shift``

        """
        if inplace:
            ts = self
        else:
            ts = copy.deepcopy(self)
        ts.time = np.asarray(ts.time) + time_shift  # type: ignore
        # Pay attention here: if the GTIs are created dynamically while we
        # access the property,
        if ts._gti is not None:
            ts._gti = np.asarray(ts._gti) + time_shift  # type: ignore

        return ts

    def _operation_with_other_obj(
        self, other, operation, operated_attrs=None, error_attrs=None, error_operation=None
    ):
        """
        Helper method to codify an operation of one time series with another (e.g. add, subtract).
        Takes into account the GTIs correctly, and returns a new :class:`StingrayTimeseries` object.

        Parameters
        ----------
        other : :class:`StingrayTimeseries` object
            A second time series object

        operation : function
            An operation between the :class:`StingrayTimeseries` object calling this method, and
            ``other``, operating on all the specified array attributes.

        Other parameters
        ----------------
        operated_attrs : list of str or None
            Array attributes to be operated on. Defaults to all array attributes not ending in
            ``_err``.
            The other array attributes will be discarded from the time series to avoid
            inconsistencies.

        error_attrs : list of str or None
            Array attributes to be operated on with ``error_operation``. Defaults to all array
            attributes ending with ``_err``.

        error_operation : function
            The function used for error propagation. Defaults to the sum of squares.

        Returns
        -------
        ts_new : StingrayTimeseries object
            The new time series calculated in ``operation``
        """

        if self.mjdref != other.mjdref:
            warnings.warn("MJDref is different in the two time series")
            other = other.change_mjdref(self.mjdref)

        if not np.array_equal(self.gti, other.gti):
            from .gti import cross_two_gtis

            warnings.warn(
                "The good time intervals in the two time series are different. Data outside the "
                "common GTIs will be discarded."
            )
            common_gti = cross_two_gtis(self.gti, other.gti)
            masked_self = self.apply_gtis(common_gti)
            masked_other = other.apply_gtis(common_gti)
            return masked_self._operation_with_other_obj(
                masked_other,
                operation,
                operated_attrs=operated_attrs,
                error_attrs=error_attrs,
                error_operation=error_operation,
            )

        return super()._operation_with_other_obj(
            other,
            operation,
            operated_attrs=operated_attrs,
            error_attrs=error_attrs,
            error_operation=error_operation,
        )

    def __add__(self, other):
        """
        Add the array values of two time series element by element, assuming they
        have the same time array.

        This magic method adds two :class:`TimeSeries` objects having the same time
        array such that the corresponding array arrays get summed up.

        GTIs are crossed, so that only common intervals are saved.

        Examples
        --------
        >>> time = [5, 10, 15]
        >>> count1 = [300, 100, 400]
        >>> count2 = [600, 1200, 800]
        >>> gti1 = [[0, 20]]
        >>> gti2 = [[0, 25]]
        >>> ts1 = StingrayTimeseries(time, array_attrs=dict(counts=count1), gti=gti1, dt=5)
        >>> ts2 = StingrayTimeseries(time, array_attrs=dict(counts=count2), gti=gti2, dt=5)
        >>> ts = ts1 + ts2
        >>> np.allclose(ts.counts, [ 900, 1300, 1200])
        True
        """

        return super().__add__(other)

    def __sub__(self, other):
        """
        Subtract the counts/flux of one time series from the counts/flux of another
        time series element by element, assuming the ``time`` arrays of the time series
        match exactly.

        This magic method adds two :class:`StingrayTimeSeries` objects having the same
        ``time`` array and subtracts the ``counts`` of one :class:`StingrayTimeseries` with
        that of another, while also updating ``countrate``, ``counts_err`` and ``countrate_err``
        correctly.

        GTIs are crossed, so that only common intervals are saved.

        Examples
        --------
        >>> time = [10, 20, 30]
        >>> count1 = [600, 1200, 800]
        >>> count2 = [300, 100, 400]
        >>> gti1 = [[0, 35]]
        >>> gti2 = [[5, 40]]
        >>> ts1 = StingrayTimeseries(time, array_attrs=dict(counts=count1), gti=gti1, dt=10)
        >>> ts2 = StingrayTimeseries(time, array_attrs=dict(counts=count2), gti=gti2, dt=10)
        >>> ts = ts1 - ts2
        >>> np.allclose(ts.counts, [ 300, 1100,  400])
        True
        """

        return super().__sub__(other)

    def __getitem__(self, index):
        """
        Return the corresponding count value at the index or a new :class:`StingrayTimeseries`
        object upon slicing.

        This method adds functionality to retrieve the count value at
        a particular index. This also can be used for slicing and generating
        a new :class:`StingrayTimeseries` object. GTIs are recalculated based on the new light
        curve segment

        If the slice object is of kind ``start:stop:step`` and ``dt`` is not 0, GTIs are also
        sliced, by crossing with ``zip(time - self.dt /2, time + self.dt / 2)``

        Parameters
        ----------
        index : int or slice instance
            Index value of the time array or a slice object.

        Examples
        --------
        >>> time = [1, 2, 3, 4, 5, 6, 7, 8, 9]
        >>> count = [11, 22, 33, 44, 55, 66, 77, 88, 99]
        >>> ts = StingrayTimeseries(time, array_attrs=dict(counts=count), dt=1)
        >>> np.allclose(ts[2].counts, [33])
        True
        >>> np.allclose(ts[:2].counts, [11, 22])
        True
        """
        from .utils import assign_value_if_none
        from .gti import cross_two_gtis

        new_ts = super().__getitem__(index)
        step = 1
        if isinstance(index, slice):
            step = assign_value_if_none(index.step, 1)

        dt = self.dt
        if np.isscalar(dt):
            delta_gti_start = delta_gti_stop = dt * 0.5
        else:
            delta_gti_start = new_ts.dt[0] * 0.5
            delta_gti_stop = new_ts.dt[-1] * 0.5

        new_gti = np.asarray([[new_ts.time[0] - delta_gti_start, new_ts.time[-1] + delta_gti_stop]])
        if step > 1 and delta_gti_start > 0:
            new_gt1 = np.array(list(zip(new_ts.time - new_ts.dt / 2, new_ts.time + new_ts.dt / 2)))
            new_gti = cross_two_gtis(new_gti, new_gt1)
        new_gti = cross_two_gtis(self.gti, new_gti)

        new_ts.gti = new_gti
        return new_ts

    def truncate(self, start=0, stop=None, method="index"):
        """
        Truncate a :class:`StingrayTimeseries` object.

        This method takes a ``start`` and a ``stop`` point (either as indices,
        or as times in the same unit as those in the ``time`` attribute, and truncates
        all bins before ``start`` and after ``stop``, then returns a new
        :class:`StingrayTimeseries` object with the truncated time series.

        Parameters
        ----------
        start : int, default 0
            Index (or time stamp) of the starting point of the truncation. If no value is set
            for the start point, then all points from the first element in the ``time`` array
            are taken into account.

        stop : int, default ``None``
            Index (or time stamp) of the ending point (exclusive) of the truncation. If no
            value of stop is set, then points including the last point in
            the counts array are taken in count.

        method : {``index`` | ``time``}, optional, default ``index``
            Type of the start and stop values. If set to ``index`` then
            the values are treated as indices of the counts array, or
            if set to ``time``, the values are treated as actual time values.

        Returns
        -------
        ts_new: :class:`StingrayTimeseries` object
            The :class:`StingrayTimeseries` object with truncated time and arrays.

        Examples
        --------
        >>> time = [1, 2, 3, 4, 5, 6, 7, 8, 9]
        >>> count = [10, 20, 30, 40, 50, 60, 70, 80, 90]
        >>> ts = StingrayTimeseries(time, array_attrs={"counts": count}, dt=1)
        >>> ts_new = ts.truncate(start=2, stop=8)
        >>> np.allclose(ts_new.counts, [30, 40, 50, 60, 70, 80])
        True
        >>> ts_new.time
        array([3, 4, 5, 6, 7, 8])
        >>> # Truncation can also be done by time values
        >>> ts_new = ts.truncate(start=6, method='time')
        >>> ts_new.time
        array([6, 7, 8, 9])
        >>> np.allclose(ts_new.counts, [60, 70, 80, 90])
        True
        """

        if not isinstance(method, str):
            raise TypeError("The method keyword argument is not a string !")

        if method.lower() not in ["index", "time"]:
            raise ValueError("Unknown method type " + method + ".")

        if method.lower() == "index":
            new_ts = self._truncate_by_index(start, stop)
        else:
            new_ts = self._truncate_by_time(start, stop)
        new_ts.tstart = new_ts.gti[0, 0]
        new_ts.tseg = new_ts.gti[-1, 1] - new_ts.gti[0, 0]
        return new_ts

    def _truncate_by_index(self, start, stop):
        """Private method for truncation using index values."""
        from .gti import cross_two_gtis

        new_ts = self.apply_mask(slice(start, stop))

        dtstart = dtstop = new_ts.dt
        if isinstance(self.dt, Iterable):
            dtstart = self.dt[0]
            dtstop = self.dt[-1]

        gti = cross_two_gtis(
            self.gti, np.asarray([[new_ts.time[0] - 0.5 * dtstart, new_ts.time[-1] + 0.5 * dtstop]])
        )

        new_ts.gti = gti

        return new_ts

    def _truncate_by_time(self, start, stop):
        """Helper method for truncation using time values.

        Parameters
        ----------
        start : float
            start time for new time series; all time bins before this time will be discarded

        stop : float
            stop time for new time series; all time bins after this point will be discarded

        Returns
        -------
            new_ts : StingrayTimeseries
                A new :class:`StingrayTimeseries` object with the truncated time bins

        """

        if stop is not None:
            if start > stop:
                raise ValueError("start time must be less than stop time!")

        if not start == 0:
            start = self.time.searchsorted(start)

        if stop is not None:
            stop = self.time.searchsorted(stop)

        return self._truncate_by_index(start, stop)

    def concatenate(self, other, check_gti=True):
        """
        Concatenate two :class:`StingrayTimeseries` objects.

        This method concatenates two or more :class:`StingrayTimeseries` objects. GTIs are
        recalculated by merging all the GTIs together. GTIs should not overlap at any point.

        Parameters
        ----------
        other : :class:`StingrayTimeseries` object or list of :class:`StingrayTimeseries` objects
            A second time series object, or a list of objects to be concatenated

        Other parameters
        ----------------
        check_gti : bool
            Check if the GTIs are overlapping or not. Default: True
            If this is True and GTIs overlap, an error is raised.
        """

        if check_gti:
            treatment = "append"
        else:
            treatment = "none"
        new_ts = self._join_timeseries(other, strategy=treatment)
        return new_ts

    def _join_timeseries(self, others, strategy="intersection", ignore_meta=[]):
        """Helper method to join two or more :class:`StingrayTimeseries` objects.

        This is a helper method that can be called by other user-facing methods, such as
        :class:`StingrayTimeseries().join()`.

        Standard attributes such as ``pi`` and ``energy`` remain ``None`` if they are ``None``
        in both. Otherwise, ``np.nan`` is used as a default value for the missing values.
        Arbitrary array attributes are created and joined using the same convention.

        Multiple checks are done on the joined time series. If the time array of the series
        being joined is empty, it is ignored (and a copy of the original time series is returned
        instead). If the time resolution is different, the final time series will associate
        different time resolutions to different time bins.
        If the MJDREF is different (including being 0), the time reference will be changed to
        the one of the first time series. An empty time series will be ignored.

        Parameters
        ----------
        other : :class:`StingrayTimeseries` or class:`list` of :class:`StingrayTimeseries`
            The other :class:`StingrayTimeseries` object which is supposed to be joined with.
            If ``other`` is a list, it is assumed to be a list of :class:`StingrayTimeseries`
            and they are all joined, one by one.

        Other parameters
        ----------------
        strategy : {"intersection", "union", "append", "infer", "none"}
            Method to use to merge the GTIs. If "intersection", the GTIs are merged
            using the intersection of the GTIs. If "union", the GTIs are merged
            using the union of the GTIs. If "none", a single GTI with the minimum and
            the maximum time stamps of all GTIs is returned. If "infer", the strategy
            is decided based on the GTIs. If there are no overlaps, "union" is used,
            otherwise "intersection" is used. If "append", the GTIs are simply appended
            but they must be mutually exclusive.

        Returns
        -------
        `ts_new` : :class:`StingrayTimeseries` object
            The resulting :class:`StingrayTimeseries` object.
        """
        from .gti import check_separate, cross_gtis, append_gtis

        new_ts = type(self)()

        if not (
            isinstance(others, Iterable)
            and not isinstance(others, str)
            and not isinstance(others, StingrayObject)
        ):
            others = [others]
        else:
            others = others

        # First of all, check if there are empty objects
        for obj in others:
            if not isinstance(obj, type(self)):
                raise TypeError(
                    f"{type(self)} objects can only be merged with other {type(self)} objects."
                )
            if getattr(obj, "time", None) is None or np.size(obj.time) == 0:
                warnings.warn("One of the time series you are joining is empty.")
                others.remove(obj)

        if len(others) == 0:
            return copy.deepcopy(self)

        for i, other in enumerate(others):
            # Tolerance for MJDREF:1 microsecond
            if not np.isclose(self.mjdref, other.mjdref, atol=1e-6 / 86400):
                warnings.warn("Attribute mjdref is different in the time series being merged.")
                others[i] = other.change_mjdref(self.mjdref)

        all_objs = [self] + others

        from .gti import merge_gtis

        # Check if none of the GTIs was already initialized.
        all_gti = [obj._gti for obj in all_objs if obj._gti is not None]

        if len(all_gti) == 0 or strategy == "none":
            new_gti = None
        else:
            # For this, initialize the GTIs
            new_gti = merge_gtis([obj.gti for obj in all_objs], strategy=strategy)

        all_time_arrays = [obj.time for obj in all_objs if obj.time is not None]

        new_ts.time = np.concatenate(all_time_arrays)
        order = np.argsort(new_ts.time)
        new_ts.time = new_ts.time[order]

        new_ts.gti = new_gti

        dts = list(set([getattr(obj, "dt", None) for obj in all_objs]))
        if len(dts) != 1:
            warnings.warn("The time resolution is different. Transforming in array")

            new_dt = np.concatenate([np.zeros_like(obj.time) + obj.dt for obj in all_objs])
            new_ts.dt = new_dt[order]
        else:
            new_ts.dt = dts[0]

        def _get_set_from_many_lists(lists):
            """Make a single set out of many lists."""
            all_vals = []
            for ls in lists:
                all_vals += ls
            return set(all_vals)

        def _get_all_array_attrs(objs):
            """Get all array attributes from the time series being merged. Do not include time."""
            return _get_set_from_many_lists(
                [obj.array_attrs() + obj.internal_array_attrs() for obj in objs]
            )

        for attr in _get_all_array_attrs(all_objs):
            # if it's here, it means that it's an array attr in at least one object.
            # So, everywhere it's None, it needs to be set to 0s of the same length as time
            new_attr_values = []
            for obj in all_objs:
                if getattr(obj, attr, None) is None:
                    warnings.warn(
                        f"The {attr} array is empty in one of the time series being merged. "
                        "Setting it to NaN for the affected events"
                    )
                    new_attr_values.append(np.zeros_like(obj.time) + np.nan)
                else:
                    new_attr_values.append(getattr(obj, attr))

            new_attr = np.concatenate(new_attr_values)[order]
            setattr(new_ts, attr, new_attr)

        all_meta_attrs = _get_set_from_many_lists([obj.meta_attrs() for obj in all_objs])
        # The attributes being treated separately are removed from the standard treatment
        # When energy, pi etc. are None, they might appear in the meta_attrs, so we
        # also add them to the list of attributes to be removed if present.
        to_remove = ["gti", "dt"] + new_ts.array_attrs()
        for attr in to_remove:
            if attr in all_meta_attrs:
                all_meta_attrs.remove(attr)

        for attr in ignore_meta:
            logging.info(f"The {attr} attribute will be removed from the output ")
            if attr in all_meta_attrs:
                all_meta_attrs.remove(attr)

        def _safe_concatenate(a, b):
            if isinstance(a, str) and isinstance(b, str):
                return a + "," + b
            else:
                if isinstance(a, tuple):
                    return a + (b,)
                return (a, b)

        for attr in all_meta_attrs:
            self_attr = getattr(self, attr, None)
            new_val = self_attr
            for other in others:
                other_attr = getattr(other, attr, None)
                if self_attr != other_attr:
                    warnings.warn(
                        "Attribute " + attr + " is different in the time series being merged."
                    )
                    new_val = _safe_concatenate(new_val, other_attr)
            setattr(new_ts, attr, new_val)

        new_ts.mjdref = self.mjdref

        return new_ts

    def join(self, *args, **kwargs):
        """
        Join other :class:`StingrayTimeseries` objects with the current one.

        If both are empty, an empty :class:`StingrayTimeseries` is returned.

        Standard attributes such as ``pi`` and ``energy`` remain ``None`` if they are ``None``
        in both. Otherwise, ``np.nan`` is used as a default value for the missing values.
        Arbitrary array attributes are created and joined using the same convention.

        Multiple checks are done on the joined time series. If the time array of the series
        being joined is empty, it is ignored. If the time resolution is different, the final
        time series will have the rougher time resolution. If the MJDREF is different, the time
        reference will be changed to the one of the first time series. An empty time series will
        be ignored.

        Parameters
        ----------
        other : :class:`StingrayTimeseries` or class:`list` of :class:`StingrayTimeseries`
            The other :class:`StingrayTimeseries` object which is supposed to be joined with.
            If ``other`` is a list, it is assumed to be a list of :class:`StingrayTimeseries`
            and they are all joined, one by one.

        Other parameters
        ----------------
        strategy : {"intersection", "union", "append", "infer", "none"}
            Method to use to merge the GTIs. If "intersection", the GTIs are merged
            using the intersection of the GTIs. If "union", the GTIs are merged
            using the union of the GTIs. If "none", a single GTI with the minimum and
            the maximum time stamps of all GTIs is returned. If "infer", the strategy
            is decided based on the GTIs. If there are no overlaps, "union" is used,
            otherwise "intersection" is used. If "append", the GTIs are simply appended
            but they must be mutually exclusive.

        Returns
        -------
        `ts_new` : :class:`StingrayTimeseries` object
            The resulting :class:`StingrayTimeseries` object.
        """
        return self._join_timeseries(*args, **kwargs)

    def rebin(self, dt_new=None, f=None, method="sum"):
        """
        Rebin the time series to a new time resolution. While the new
        resolution need not be an integer multiple of the previous time
        resolution, be aware that if it is not, the last bin will be cut
        off by the fraction left over by the integer division.

        Parameters
        ----------
        dt_new: float
            The new time resolution of the time series. Must be larger than
            the time resolution of the old time series!

        method: {``sum`` | ``mean`` | ``average``}, optional, default ``sum``
            This keyword argument sets whether the counts in the new bins
            should be summed or averaged.

        Other Parameters
        ----------------
        f: float
            the rebin factor. If specified, it substitutes ``dt_new`` with
            ``f*self.dt``

        Returns
        -------
        ts_new: :class:`StingrayTimeseries` object
            The :class:`StingrayTimeseries` object with the new, binned time series.
        """
        from .utils import rebin_data

        if f is None and dt_new is None:
            raise ValueError("You need to specify at least one between f and " "dt_new")
        elif f is not None:
            dt_new = f * self.dt

        if dt_new < self.dt:
            raise ValueError("The new time resolution must be larger than the old one!")

        gti_new = []

        new_ts = type(self)()

        for attr in self.array_attrs() + self.internal_array_attrs():
            bin_time, bin_counts, bin_err = [], [], []
            if attr.endswith("_err"):
                continue
            e_temp = None
            for g in self.gti:
                if g[1] - g[0] < dt_new:
                    continue
                else:
                    # find start and end of GTI segment in data
                    start_ind = self.time.searchsorted(g[0])
                    end_ind = self.time.searchsorted(g[1])

                    t_temp = self.time[start_ind:end_ind]
                    c_temp = getattr(self, attr)[start_ind:end_ind]

                    if hasattr(self, attr + "_err"):
                        e_temp = getattr(self, attr + "_err")[start_ind:end_ind]

                    bin_t, bin_c, bin_e, _ = rebin_data(
                        t_temp, c_temp, dt_new, yerr=e_temp, method=method
                    )

                    bin_time.extend(bin_t)
                    bin_counts.extend(bin_c)
                    bin_err.extend(bin_e)
                    gti_new.append(g)
            if new_ts.time is None:
                new_ts.time = np.array(bin_time)
            setattr(new_ts, attr, bin_counts)
            if e_temp is not None:
                setattr(new_ts, attr + "_err", bin_err)

        if len(gti_new) == 0:
            raise ValueError("No valid GTIs after rebin.")
        new_ts.gti = np.asarray(gti_new)

        for attr in self.meta_attrs():
            if attr == "dt":
                continue
            setattr(new_ts, attr, copy.deepcopy(getattr(self, attr)))
        new_ts.dt = dt_new
        return new_ts

    def sort(self, reverse=False, inplace=False):
        """
        Sort a ``StingrayTimeseries`` object by time.

        A ``StingrayTimeserie``s can be sorted in either increasing or decreasing order
        using this method. The time array gets sorted and the counts array is
        changed accordingly.

        Parameters
        ----------
        reverse : boolean, default False
            If True then the object is sorted in reverse order.
        inplace : bool
            If True, overwrite the current time series. Otherwise, return a new one.

        Examples
        --------
        >>> time = [2, 1, 3]
        >>> count = [200, 100, 300]
        >>> ts = StingrayTimeseries(time, array_attrs={"counts": count}, dt=1)
        >>> ts_new = ts.sort()
        >>> ts_new.time
        array([1, 2, 3])
        >>> np.allclose(ts_new.counts, [100, 200, 300])
        True

        Returns
        -------
        ts_new: :class:`StingrayTimeseries` object
            The :class:`StingrayTimeseries` object with sorted time and counts
            arrays.
        """

        mask = np.argsort(self.time)
        if reverse:
            mask = mask[::-1]
        return self.apply_mask(mask, inplace=inplace)

    def plot(
        self,
        attr,
        witherrors=False,
        labels=None,
        ax=None,
        title=None,
        marker="-",
        save=False,
        filename=None,
        plot_btis=True,
    ):
        """
        Plot the time series using ``matplotlib``.

        Plot the time series object on a graph ``self.time`` on x-axis and
        ``self.counts`` on y-axis with ``self.counts_err`` optionally
        as error bars.

        Parameters
        ----------
        attr: str
            Attribute to plot.

        Other parameters
        ----------------
        witherrors: boolean, default False
            Whether to plot the StingrayTimeseries with errorbars or not
        labels : iterable, default ``None``
            A list or tuple with ``xlabel`` and ``ylabel`` as strings. E.g.
            if the attribute is ``'counts'``, the list of labels
            could be ``['Time (s)', 'Counts (s^-1)']``
        ax : ``matplotlib.pyplot.axis`` object
            Axis to be used for plotting. Defaults to creating a new one.
        title : str, default ``None``
            The title of the plot.
        marker : str, default '-'
            Line style and color of the plot. Line styles and colors are
            combined in a single format string, as in ``'bo'`` for blue
            circles. See ``matplotlib.pyplot.plot`` for more options.
        save : boolean, optional, default ``False``
            If ``True``, save the figure with specified filename.
        filename : str
            File name of the image to save. Depends on the boolean ``save``.
        plot_btis : bool
            Plot the bad time intervals as red areas on the plot
        """
        import matplotlib.pyplot as plt
        from .gti import get_btis

        if ax is None:
            plt.figure(attr)
            ax = plt.gca()

        if labels is None:
            labels = ["Time (s)"] + [attr]

        ylabel = labels[1]
        xlabel = labels[0]

        ax.plot(self.time, getattr(self, attr), marker, ds="steps-mid", label=attr, zorder=10)

        if witherrors and attr + "_err" in self.array_attrs():
            ax.errorbar(
                self.time,
                getattr(self, attr),
                yerr=getattr(self, attr + "_err"),
                fmt="o",
            )

        ax.set_ylabel(ylabel)
        ax.set_xlabel(xlabel)

        if title is not None:
            ax.set_title(title)

        if save:
            if filename is None:
                ax.figure.savefig("out.png")
            else:
                ax.figure.savefig(filename)

        if plot_btis and self.gti is not None and len(self.gti) > 1:
            tstart = min(self.time[0] - self.dt / 2, self.gti[0, 0])
            tend = max(self.time[-1] + self.dt / 2, self.gti[-1, 1])
            btis = get_btis(self.gti, tstart, tend)
            for bti in btis:
                plt.axvspan(bti[0], bti[1], alpha=0.5, color="r", zorder=10)
        return ax


def interpret_times(time: TTime, mjdref: float = 0) -> tuple[npt.ArrayLike, float]:
    """Understand the format of input times, and return seconds from MJDREF

    Parameters
    ----------
    time : class:`astropy.Time`, class:`time.Time`, class:`astropy.TimeDelta`, class:`astropy.Quantity`, class:`np.array`
        Input times.

    Other Parameters
    ----------------
    mjdref : float
        Input MJD reference of the times. Optional.

    Returns
    -------
    time_s : class:`np.array`
        Times, in seconds from MJDREF
    mjdref : float
        MJDREF. If the input time is a `time.Time` object and the input mjdref
        is 0, it will be defined as the MJD of the input time.

    Examples
    --------
    >>> import astropy.units as u
    >>> newt, mjdref = interpret_times(None)
    >>> newt is None
    True
    >>> time = Time(57483, format='mjd')
    >>> newt, mjdref = interpret_times(time)
    >>> newt == 0
    True
    >>> mjdref == 57483
    True
    >>> time = Time([57483], format='mjd')
    >>> newt, mjdref = interpret_times(time)
    >>> np.allclose(newt, 0)
    True
    >>> mjdref == 57483
    True
    >>> time = TimeDelta([3, 4, 5] * u.s)
    >>> newt, mjdref = interpret_times(time)
    >>> np.allclose(newt, [3, 4, 5])
    True
    >>> time = np.array([3, 4, 5])
    >>> newt, mjdref = interpret_times(time, mjdref=45000)
    >>> np.allclose(newt, [3, 4, 5])
    True
    >>> mjdref == 45000
    True
    >>> time = np.array([3, 4, 5] * u.s)
    >>> newt, mjdref = interpret_times(time, mjdref=45000)
    >>> np.allclose(newt, [3, 4, 5])
    True
    >>> mjdref == 45000
    True
    >>> newt, mjdref = interpret_times(1, mjdref=45000)
    >>> newt == 1
    True
    >>> newt, mjdref = interpret_times(list, mjdref=45000)
    Traceback (most recent call last):
    ...
    ValueError: Unknown time format: ...
    >>> newt, mjdref = interpret_times("guadfkljfd", mjdref=45000)
    Traceback (most recent call last):
    ...
    ValueError: Unknown time format: ...
    """
    if time is None:
        return None, mjdref

    if isinstance(time, TimeDelta):
        out_times = time.to("s").value
        return out_times, mjdref

    if isinstance(time, Time):
        mjds = time.mjd
        if mjdref == 0:
            if np.all(mjds > 10000):
                if isinstance(mjds, Iterable):
                    mjdref = mjds[0]
                else:
                    mjdref = mjds

        out_times = (mjds - mjdref) * 86400
        return out_times, mjdref

    if isinstance(time, Quantity):
        out_times = time.to("s").value
        return out_times, mjdref

    if isinstance(time, (tuple, list, np.ndarray)):
        return time, mjdref

    if not isinstance(time, Iterable):
        try:
            float(time)
            return time, mjdref
        except (ValueError, TypeError):
            pass

    raise ValueError(f"Unknown time format: {type(time)}")


def reduce_precision_if_extended(
    x, probe_types=["float128", "float96", "float80", "longdouble"], destination=float
):
    """Reduce a number to a standard float if extended precision.

    Ignore all non-float types.

    Parameters
    ----------
    x : float
        The number to be reduced

    Returns
    -------
    x_red : same type of input
        The input, only reduce to ``float`` precision if ``np.float128``

    Examples
    --------
    >>> x = 1.0
    >>> val = reduce_precision_if_extended(x, probe_types=["float64"])
    >>> val is x
    True
    >>> x = np.asanyarray(1.0).astype(int)
    >>> val = reduce_precision_if_extended(x, probe_types=["float64"])
    >>> val is x
    True
    >>> x = np.asanyarray([1.0]).astype(int)
    >>> val = reduce_precision_if_extended(x, probe_types=["float64"])
    >>> val is x
    True
    >>> x = np.asanyarray(1.0).astype(np.float64)
    >>> reduce_precision_if_extended(x, probe_types=["float64"], destination=np.float32) is x
    False
    >>> x = np.asanyarray([1.0]).astype(np.float64)
    >>> reduce_precision_if_extended(x, probe_types=["float64"], destination=np.float32) is x
    False
    """
    if any([t in str(np.obj2sctype(x)) for t in probe_types]):
        x_ret = x.astype(destination)
        return x_ret
    return x
