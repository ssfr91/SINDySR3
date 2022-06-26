import warnings

import numpy as np
from scipy.linalg import cho_factor
from scipy.linalg import cho_solve
from scipy.optimize import bisect
from sklearn.exceptions import ConvergenceWarning

from pysindy.optimizers import BaseOptimizer
from pysindy.utils import get_prox
from utils import get_reg


class SR3Enhanced(BaseOptimizer):
    """
    Sparse relaxed regularized regression (SR3) with corrupt data
    trimming and/or constraints.

    Parameters
    ----------
    threshold : float, optional (default 0.1)
        Determines the strength of the regularization. When the
        regularization function R is the l0 norm, the regularization
        is equivalent to performing hard thresholding, and lambda
        is chosen to threshold at the value given by this parameter.
        This is equivalent to choosing lambda = threshold^2 / (2 * nu).

    nu : float, optional (default 1)
        Determines the level of relaxation. Decreasing nu encourages
        w and v to be close, whereas increasing nu allows the
        regularized coefficients v to be farther from w.

    tol : float, optional (default 1e-5)
        Tolerance used for determining convergence of the optimization
        algorithm.

    thresholder : string, optional (default 'l0')
        Regularization function to use. Currently implemented options
        are 'l0' (l0 norm), 'l1' (l1 norm), and 'cad' (clipped
        absolute deviation).

    trimming_fraction : float, optional (default 0.0)
        Fraction of the data samples to trim during fitting. Should
        be a float between 0.0 and 1.0. If 0.0, trimming is not
        performed.

    trimming_initialization : array, shape (n_samples,) (default None)
        Optional initialization of trimming weights, which represent
        the likelihood of each sample to be kept. All values should be
        nonnegative and the values in the array should sum to
        trimming_fraction * n_samples. If None, all samples are
        weighted equally.
    
    trimming_step_size : float, optional (default 1.0)
        Step size to use in the trimming optimization procedure.

    constraint_lhs : array, shape (n_constraints, n_targets * n_features) (default None)
        Matrix specifying a set of linear constraints to be imposed on
        the SINDy coefficients. If this parameter is passed in,
        constraints_rhs must be as well. After the fitting, full
        coefficients should satisfy
        np.dot(constraint_lhs, coef_full_.flatten()) = constraint_rhs.

    constraint_rhs : array, shape (n_constraints,) (default None)
        Right hand side of the linear constraints to be imposed on the
        SINDy coefficients. If this parameter is passed in,
        constraints_lhs must be as well.

    max_iter : int, optional (default 30)
        Maximum iterations of the optimization algorithm.

    fit_intercept : boolean, optional (default False)
        Whether to calculate the intercept for this model. If set to false, no
        intercept will be used in calculations.

    normalize : boolean, optional (default False)
        This parameter is ignored when fit_intercept is set to False. If True,
        the regressors X will be normalized before regression by subtracting
        the mean and dividing by the l2-norm.

    copy_X : boolean, optional (default True)
        If True, X will be copied; else, it may be overwritten.

    Attributes
    ----------
    coef_ : array, shape (n_features,) or (n_targets, n_features)
        Regularized weight vector(s). This is the v in the objective
        function.

    coef_full_ : array, shape (n_features,) or (n_targets, n_features)
        Weight vector(s) that are not subjected to the regularization.
        This is the w in the objective function.

    trimming_array_ : array, shape (n_samples,)
        If trimming was performed, this is an array specifying which values
        were kept and which were trimmed in the trimming procedure.
    """

    def __init__(
        self,
        threshold=0.1,
        nu=1.0,
        tol=1e-5,
        thresholder="l0",
        max_iter=30,
        trimming_fraction=0.0,
        trimming_initialization=None,
        trimming_step_size=1.0,
        constraint_lhs=None,
        constraint_rhs=None,
        normalize_columns=False,
        fit_intercept=False,
        copy_X=True,
    ):
        super(SR3Enhanced, self).__init__(
            max_iter=max_iter,
            normalize_columns=normalize_columns,
            fit_intercept=fit_intercept,
            copy_X=copy_X,
        )

        if threshold < 0:
            raise ValueError("threshold cannot be negative")
        if nu <= 0:
            raise ValueError("nu must be positive")
        if tol <= 0:
            raise ValueError("tol must be positive")

        self.threshold = threshold
        self.nu = nu
        self.tol = tol
        self.thresholder = thresholder
        self.prox = get_prox(thresholder)
        self.reg = get_reg(thresholder)
        if trimming_fraction == 0.0:
            self.use_trimming = False
        else:
            self.use_trimming = True
            self.trimming_fraction = trimming_fraction
            self.trimming_initialization = trimming_initialization
            self.trimming_step_size = trimming_step_size
        self.use_constraints = (constraint_lhs is not None) and (
            constraint_rhs is not None
        )
        if self.use_constraints:
            self.n_constraints = constraint_lhs.shape[0]
            self.constraint_lhs = constraint_lhs
            self.constraint_rhs = constraint_rhs

    def enable_trimming(self, trimming_fraction):
        self.use_trimming = True
        self.trimming_fraction = trimming_fraction

    def disable_trimming(self):
        self.use_trimming = False
        self.trimming_fraction = None

    def _update_full_coef(self, cho, x_transpose_y, coef_sparse):
        """Update the unregularized weight vector
        """
        b = x_transpose_y + coef_sparse / self.nu
        coef_full = cho_solve(cho, b)
        self.iters += 1
        return coef_full

    def _update_full_coef_constraints(self, H, x_transpose_y, coef_sparse):
        g = x_transpose_y + coef_sparse / self.nu
        inv1 = np.linalg.inv(H)
        inv1_mod = np.kron(inv1, np.eye(coef_sparse.shape[1]))
        inv2 = np.linalg.inv(
            self.constraint_lhs.dot(inv1_mod).dot(self.constraint_lhs.T)
        )

        rhs = g.flatten() + self.constraint_lhs.T.dot(inv2).dot(
            self.constraint_rhs - self.constraint_lhs.dot(inv1_mod).dot(g.flatten())
        )
        rhs = rhs.reshape(g.shape)
        return inv1.dot(rhs)

    def _update_sparse_coef(self, coef_full):
        """Update the regularized weight vector
        """
        coef_sparse = self.prox(coef_full, self.threshold)
        self.history_.append(coef_sparse.T)
        return coef_sparse

    def _update_trimming_array(self, coef_full, trimming_array, trimming_grad):
        trimming_array = trimming_array - self.trimming_step_size * trimming_grad
        trimming_array = self.cSimplexProj(trimming_array, self.trimming_fraction)
        self.history_trimming_.append(trimming_array)
        return trimming_array

    def _trimming_grad(self, x, y, coef_full, trimming_array):
        """gradient for the trimming variable"""
        R2 = (y - x.dot(coef_full)) ** 2
        return 0.5 * np.sum(R2, axis=1)

    def _objective(self, x, y, coef_full, coef_sparse, trimming_array=None):
        """objective function"""
        R2 = (y - np.dot(x, coef_full)) ** 2
        D2 = (coef_full - coef_sparse) ** 2
        if self.use_trimming:
            assert trimming_array is not None
            R2 *= trimming_array.reshape(x.shape[0], 1)

        return (
            0.5 * np.sum(R2)
            + self.reg(coef_full, 0.5 * self.threshold ** 2 / self.nu)
            + 0.5 * np.sum(D2) / self.nu
        )

    def _convergence_criterion(self):
        """Calculate the convergence criterion for the optimization
        """
        this_coef = self.history_[-1]
        if len(self.history_) > 1:
            last_coef = self.history_[-2]
        else:
            last_coef = np.zeros_like(this_coef)
        err_coef = np.sqrt(np.sum((this_coef - last_coef) ** 2)) / self.nu
        if self.use_trimming:
            this_trimming_array = self.history_trimming_[-1]
            if len(self.history_trimming_) > 1:
                last_trimming_array = self.history_trimming_[-2]
            else:
                last_trimming_array = np.zeros_like(this_trimming_array)
            err_trimming = (
                np.sqrt(np.sum((this_trimming_array - last_trimming_array) ** 2))
                / self.trimming_step_size
            )
            return err_coef + err_trimming
        return err_coef

    def _reduce(self, x, y):
        coef_sparse = self.coef_.T
        n_samples, n_features = x.shape

        if self.use_trimming:
            coef_full = coef_sparse.copy()
            assert self.trimming_fraction != 0
            if self.trimming_initialization is None:
                self.trimming_initialization = np.repeat(
                    self.trimming_fraction, n_samples
                )
            assert (
                np.abs(
                    np.sum(self.trimming_initialization)
                    - self.trimming_fraction * n_samples
                )
                <= 1e-6 * n_samples
            )
            assert np.all(
                (self.trimming_initialization >= 0.0)
                & (self.trimming_initialization <= 1.0)
            )
            trimming_array = self.trimming_initialization.copy()
            self.history_trimming_ = [trimming_array]

        # Precompute some objects for upcoming least-squares solves.
        # Assumes that self.nu is fixed throughout optimization procedure.
        H = np.dot(x.T, x) + np.diag(np.full(x.shape[1], 1.0 / self.nu))
        x_transpose_y = np.dot(x.T, y)
        if not self.use_constraints:
            cho = cho_factor(H)

        obj_his = []
        for _ in range(self.max_iter):
            if self.use_trimming:
                x_weighted = x * trimming_array.reshape(n_samples, 1)
                H = np.dot(x_weighted.T, x) + np.diag(
                    np.full(x.shape[1], 1.0 / self.nu)
                )
                x_transpose_y = np.dot(x_weighted.T, y)
                if not self.use_constraints:
                    cho = cho_factor(H)
                trimming_grad = 0.5 * np.sum((y - x.dot(coef_full)) ** 2, axis=1)
            if self.use_constraints:
                coef_full = self._update_full_coef_constraints(
                    H, x_transpose_y, coef_sparse
                )
            else:
                coef_full = self._update_full_coef(cho, x_transpose_y, coef_sparse)
            coef_sparse = self._update_sparse_coef(coef_full)
            if self.use_trimming:
                trimming_array = self._update_trimming_array(
                    coef_full, trimming_array, trimming_grad
                )

            if self.use_trimming:
                obj_his.append(
                    self._objective(x, y, coef_full, coef_sparse, trimming_array)
                )
            else:
                obj_his.append(self._objective(x, y, coef_full, coef_sparse))
            if self._convergence_criterion() < self.tol:
                # NOTE: HAVEN'T UPDATED THIS FOR TRIMMING/CONSTRAINTS YET
                break
        else:
            warnings.warn(
                "SR3._reduce did not converge after {} iterations.".format(
                    self.max_iter
                ),
                ConvergenceWarning,
            )

        self.coef_ = coef_sparse.T
        self.coef_full_ = coef_full.T
        if self.use_trimming:
            self.trimming_array = trimming_array
        self.obj_his = obj_his

    @staticmethod
    def cSimplexProj(trimming_array, trimming_fraction):
        """projected onto the capped simplex"""
        a = np.min(trimming_array) - 1.0
        b = np.max(trimming_array) - 0.0

        def f(x):
            return (
                np.sum(np.maximum(np.minimum(trimming_array - x, 1.0), 0.0))
                - trimming_fraction * trimming_array.size
            )

        x = bisect(f, a, b)

        return np.maximum(np.minimum(trimming_array - x, 1.0), 0.0)
