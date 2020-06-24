import sys
from itertools import groupby
from warnings import warn
import numpy as np
from scipy.sparse import find, coo_matrix
from inspect import isfunction

EPS = np.finfo(float).eps


def validate_first_step(first_step, t0, t_bound):
    """Assert that first_step is valid and return it."""
    if first_step <= 0:
        raise ValueError("`first_step` must be positive.")
    if first_step > np.abs(t_bound - t0):
        raise ValueError("`first_step` exceeds bounds.")
    return first_step


def validate_max_step(max_step):
    """Assert that max_Step is valid and return it."""
    if max_step <= 0:
        raise ValueError("`max_step` must be positive.")
    return max_step


def warn_extraneous(extraneous):
    """Display a warning for extraneous keyword arguments.

    The initializer of each solver class is expected to collect keyword
    arguments that it doesn't understand and warn about them. This function
    prints a warning for each key in the supplied dictionary.

    Parameters
    ----------
    extraneous : dict
        Extraneous keyword arguments
    """
    if extraneous:
        warn("The following arguments have no effect for a chosen solver: {}."
             .format(", ".join("`{}`".format(x) for x in extraneous)))


def validate_tol(rtol, atol, n):
    """Validate tolerance values."""
    if rtol < 100 * EPS:
        warn("`rtol` is too low, setting to {}".format(100 * EPS))
        rtol = 100 * EPS

    atol = np.asarray(atol)
    if atol.ndim > 0 and atol.shape != (n,):
        raise ValueError("`atol` has wrong shape.")

    if np.any(atol < 0):
        raise ValueError("`atol` must be positive.")

    return rtol, atol


def norm(x):
    """Compute RMS norm."""
    return np.linalg.norm(x) / x.size ** 0.5


def select_initial_step(fun, t0, y0, Z0, f0, direction, order, rtol, atol):
    """Empirically select a good initial step.

    The algorithm is described in [1]_.

    Parameters
    ----------
    fun : callable
        Right-hand side of the system.
    t0 : float
        Initial value of the independent variable.
    y0 : ndarray, shape (n,)
        Initial value of the dependent variable.
    f0 : ndarray, shape (n,)
        Initial value of the derivative, i.e., ``fun(t0, y0)``.
    direction : float
        Integration direction.
    order : float
        Error estimator order. It means that the error controlled by the
        algorithm is proportional to ``step_size ** (order + 1)`.
    rtol : float
        Desired relative tolerance.
    atol : float
        Desired absolute tolerance.

    Returns
    -------
    h_abs : float
        Absolute value of the suggested initial step.

    References
    ----------
    .. [1] E. Hairer, S. P. Norsett G. Wanner, "Solving Ordinary Differential
           Equations I: Nonstiff Problems", Sec. II.4.
    """
    if y0.size == 0:
        return np.inf

    scale = atol + np.abs(y0) * rtol
    d0 = norm(y0 / scale)
    d1 = norm(f0 / scale)
    if d0 < 1e-5 or d1 < 1e-5:
        h0 = 1e-6
    else:
        h0 = 0.01 * d0 / d1

    y1 = y0 + h0 * direction * f0
    f1 = fun(t0 + h0 * direction, y1, Z0)
    d2 = norm((f1 - f0) / scale) / h0

    if d1 <= 1e-15 and d2 <= 1e-15:
        h1 = max(1e-6, h0 * 1e-3)
    else:
        h1 = (0.01 / max(d1, d2)) ** (1 / (order + 1))

    return min(100 * h0, h1)


class ContinuousExt(object):
    """Continuous DDE solution.

    It is organized as a collection of `DenseOutput` objects which represent
    local interpolants. It provides an algorithm to select a right interpolant
    for each given point.

    The interpolants cover the range between `t_min` and `t_max` (see
    Attributes below). 
    
    ***************
    Evaluation outside this interval have to be forbidden, but
    not yet implemented
    *********

    When evaluating at a breakpoint (one of the values in `ts`) a segment with
    the lower index is selected.

    Parameters
    ----------
    ts : array_like, shape (n_segments + 1,)
        Time instants between which local interpolants are defined. Must
        be strictly increasing or decreasing (zero segment with two points is
        also allowed).
    interpolants : list of history and DenseOutput with respectively 1 and 
        n_segments-1 elements
        Local interpolants. An i-th interpolant is assumed to be defined
        between ``ts[i]`` and ``ts[i + 1]``.
    ys : array_like, shape (n_segments + 1,)
        variables values associated to ts
    Attributes
    ----------
    t_min, t_max : float
        Time range of the interpolation.
    """
    def __init__(self, ts, interpolants, ys):
        self.repeated_t = False
        if np.any(np.ediff1d(ts) < EPS):
            # where is at least 2 repeated time in ts
            self.repeated_t = True
            # locate duplicate values  
            # print('type(ts)', type(ts))
            self.idxs = np.argwhere(np.diff(ts) < EPS) + 1
            # print('idxs', self.idxs)
            idxs = self.idxs[:,0].tolist()
            # print('pos', pos, 'type pos', type(pos))
            # save discont values
            self.t_discont = [ts[i] for i in idxs]
            self.y_discont = [ys[i] for i in idxs]
            # print('self.t_discont', self.t_discont,'self.y_discont', self.y_discont)
            # del discont values
            # print('len(ts)', len(ts), 'len(ys)',len(ys))
            # for k in range(len(pos)):
                # print('self.t_discont k', self.t_discont[k])
            ts = [i for j, i in enumerate(ts) if j not in idxs]
            ys =[i for j, i in enumerate(ys) if j not in idxs]
            # print('len(ts)', len(ts), 'len(ys)',len(ys))
        ts = np.asarray(ts)
        self.ys = ys
        d = np.diff(ts)
        # print('ts',ts, '\nd', d)
        # The first case covers integration on zero segment.
        # print('ts[0]==ts[-1]', ts[0]==ts[-1],'ts[0]',ts[0],'ts[-1]',ts[-1],'ts.size == 2',ts.size == 2)
        # print('(np.all(d > 0)', (np.all(d > 0)), '(np.all(d < 0)', (np.all(d < 0)))
        incr_decr = (np.all(d > 0) or np.all(d < 0))
        # print('incr_decr', incr_decr)
        if not ((ts.size == 2 and ts[0] == ts[-1]) or incr_decr):
            raise ValueError("`ts` must be strictly increasing or decreasing.")

        self.n_segments = len(interpolants)
        if ts.shape != (self.n_segments + 1,) or len(ts) != len(ys):
            raise ValueError("Numbers of time stamps and interpolants "
                             "don't match.")
        if len(ts) != len(ys):
            raise ValueError("number of ys and ts "
                             "don't match.")

        self.ts = ts
        self.interpolants = interpolants
        if ts[-1] >= ts[0]:
            self.t_min = ts[0]
            self.t_max = ts[-1]
            self.ascending = True
            self.ts_sorted = ts
        else:
            self.t_min = ts[-1]
            self.t_max = ts[0]
            self.ascending = False
            self.ts_sorted = ts[::-1]

    def _call_single(self, t):
        # Here we preserve a certain symmetry that when t is in self.ts,
        # then we prioritize a segment with a lower index.

        # if discont case + t is a discont 
        if self.repeated_t and np.any(np.abs(self.t_discont - t) < EPS):
            print('return the discont value at t=%s' % t)
            if self.ascending:
                ind = np.searchsorted(self.t_discont, t, side='left')
            else:
                ind = np.searchsorted(self.t_discont, t, side='right')
            return self.y_discont[ind]

        if self.ascending:
            ind = np.searchsorted(self.ts_sorted, t, side='left')
        else:
            ind = np.searchsorted(self.ts_sorted, t, side='right')

        segment = min(max(ind - 1, 0), self.n_segments - 1)
        if not self.ascending:
            segment = self.n_segments - 1 - segment

        if(segment==0):# and
#           self.interpolants[segment].__class__.__name__ != 'RkDenseOutput'):
            history = self.interpolants[segment]
            # as we store history values between t0-delayMax and t0
            # the first segment is not a dense output of the RK integration
            # a specific management is needed
            if(type(history) is list):
                # this is list on cubicHermiteSpline
                n = len(history)
                va = np.zeros(n)
                for k in range(n):
                    va[k] = history[k](t)
            elif(isfunction(history)):
                # from a function
                va = history(t)
            elif(isinstance(history, np.ndarray)):
                # from a cte
                va = history
            return va
        else:
            return self.interpolants[segment](t)

    def __call__(self, t):
        """Evaluate the solution.

        Parameters
        ----------
        t : float or array_like with shape (n_points,)
            Points to evaluate at.

        Returns
        -------
        y : ndarray, shape (n_states,) or (n_states, n_points)
            Computed values. Shape depends on whether `t` is a scalar or a
            1-D array.
        """
        t = np.asarray(t)
        if t.ndim == 0:
            return self._call_single(t)

        # order = np.argsort(t)
        # reverse = np.empty_like(order)
        # reverse[order] = np.arange(order.shape[0])
        # t_sorted = t[order]

        # check if repeated time at construction and if t given by user have
        # repeated values too
        if self.repeated_t:
            if not np.any(np.diff(t) < EPS):
                raise ValueError("as discontinuities within tspan "
                                 "the user have to provide t with disconts")
            # if it is the case .... we remove repeated time to add them at 
            # the end of interp process
            idxs = np.argwhere(np.diff(t) < EPS) + 1
            t = np.delete(t, idxs)
            order = np.argsort(t)
            reverse = np.empty_like(order)
            reverse[order] = np.arange(order.shape[0])
            t_sorted = t[order]
        else:
            order = np.argsort(t)
            reverse = np.empty_like(order)
            reverse[order] = np.arange(order.shape[0])
            t_sorted = t[order]


        # See comment in self._call_single.
        if self.ascending:
            segments = np.searchsorted(self.ts_sorted, t_sorted, side='left')
        else:
            segments = np.searchsorted(self.ts_sorted, t_sorted, side='right')
        segments -= 1
        segments[segments < 0] = 0
        segments[segments > self.n_segments - 1] = self.n_segments - 1
        if not self.ascending:
            segments = self.n_segments - 1 - segments

        ys = []
        group_start = 0
        for segment, group in groupby(segments):
            group_end = group_start + len(list(group))
            # added code for history segment
            if(segment==0):
                Nt = len(t_sorted[group_start:group_end])
                interp = self.interpolants[segment]
                n = len(self.ys[0])
                y = np.zeros((n,Nt))
                for k in range(Nt):
                    if(type(interp) is list):
                        # this is list on cubicHermiteSpline
                        va = np.zeros(n)
                        for m in range(n):
                            va[m] = interp[m](t[k])
                    elif(isfunction(interp)):
                        # from a function
                        va = interp(t[k])
                    elif(isinstance(interp, np.ndarray)):
                        # from a cte
                        va = interp
                    y[:,k] = va
            else:
                y = self.interpolants[segment](t_sorted[group_start:group_end])
            ys.append(y)
            group_start = group_end

        ys = np.hstack(ys)
        ys = ys[:, reverse]

        # if(self.repeated_t and np.any(np.argwhere(np.diff(t) < EPS))):
        if self.repeated_t:
            print('*********')
            print('Note: discont managment \n provide time intervals \
                   with duplicate times at discontinuities')
            print('*********')
            # adding the discont value at t_discont
            # print('t_sorted', t_sorted)
            t_tmp = t_sorted.copy()
            for k in range(len(self.idxs)):
                idx = np.searchsorted(t_tmp, self.t_discont[k]) + 1
                t_tmp = np.insert(t_tmp, idx, self.t_discont[k])
                ys = np.insert(ys, idx, self.y_discont[k], axis=1)
        return ys

    def reorganize(self, t0_new):
        """
            used file base.py, function : init_history_function
        """
        print('t0_new', t0_new)
        idx = np.searchsorted(self.ts, t0_new, 'left') + 1
        self.ts = self.ts[:idx]
        self.interpolants = self.interpolants[:idx]
        # print('self.ts[idx]', self.ts[idx])
        # print('self.ts[idx+1]', self.ts[:idx+1], 'len(self.ts[idx+1])', len(self.ts[:idx+1]))
        # print('len(self.interpolants[idx])', len(self.interpolants[:idx])) 
