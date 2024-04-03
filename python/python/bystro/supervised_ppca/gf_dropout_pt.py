"""
This implements Factor analysis but with variational inference to allow us
to supervise a subset of latent variables to be predictive of an auxiliary 
outcome. Currently only implements logistic regression but in the future 
will be modified to allow for more general predictive outcomes.

Note that in the code W is a L x p array, where L is the latent dimensionality
and  p is the covariate dimension, for implementation convenience. However,
mathematically it is often pxL for notational convenience. Given that the
most insidious errors are mathematical in nature rather than coding, (as faulty
math is difficult to detect in unit tests), our notation matches mathematics
rather than code, specifically when naming WWT and WTW to match the Bishop 2006
notation rather than code.


Objects
-------
PPCADropout(PPCA)
    This is PPCA with SGD but supervises a single factor to predict y.

Methods
-------
None
"""
import numpy as np
from numpy.typing import NDArray

from tqdm import trange
import torch
from torch import Tensor, nn
from torch.distributions.multivariate_normal import MultivariateNormal
from sklearn.linear_model import LogisticRegression

from bystro.supervised_ppca.gf_generative_pt import PPCA


def _get_projection_matrix(W_: Tensor, sigma_: Tensor):
    """
    This is currently just implemented for PPCA due to nicer formula. Will
    modify for broader application later.

    Computes the parameters for p(S|X)

    Description in future to be released paper

    Parameters
    ----------
    W_ : Tensor(n_components,p)
        The loadings

    sigma_ : Tensor
        Isotropic noise

    Returns
    -------
    Proj_X : Tensor(n_components,p)
        Beta such that np.dot(Proj_X, X) = E[S|X]

    Cov : Tensor(n_components,n_components)
        Var(S|X)
    """
    n_components = int(W_.shape[0])
    eye = torch.tensor(np.eye(n_components).astype(np.float32))
    M_init = torch.matmul(W_, torch.transpose(W_, 0, 1))
    M_end = sigma_ * eye
    M = M_init + M_end
    Proj_X = torch.linalg.solve(M, W_)
    Cov = torch.linalg.inv(M) * sigma_
    return Proj_X, Cov


class PPCADropout(PPCA):
    """
    This implements supervised PPCA according to the paper draft in
    prepration (Talbot et al, 2023), robust variational inference with
    variational objectivesThat is the generative mechanism matches
    probabilistic PCA with  isotropic variance. However, a variational
    lower bound ona predictive objective is used to ensure that a subset
    of latent variables are predictive of an auxiliary task.

    Parameters
    ----------
    n_components : int
        The latent dimensionality

    n_supervised : int
        The number of predictive latent variables

    prior_options : dict
        The hyperparameters for the prior on the parameters

    mu : float>0,default=1.0

    gamma : float,default=10.0

    delta : 5.0


    """

    def __init__(
        self,
        n_components: int = 2,
        n_supervised: int = 1,
        prior_options: dict | None = None,
        mu: float = 1.0,
        gamma: float = 10.0,
        delta: float = 5.0,
        training_options: dict | None = None,
    ):
        self.mu = float(mu)
        self.gamma = float(gamma)
        self.delta = float(delta)
        self.n_supervised = int(n_supervised)
        super().__init__(
            n_components=n_components,
            prior_options=prior_options,
            training_options=training_options,
        )
        self._initialize_save_losses()
        self.losses_supervision = np.empty(
            self.training_options["n_iterations"]
        )

    # override needed for mypy to ignore the non-optional `y` argument
    def fit(  # type: ignore[override]
        self,
        X: NDArray[np.float_],
        y: NDArray[np.float_],
        task: str = "classification",
        progress_bar: bool = True,
        seed: int = 2021,
    ) -> "PPCADropout":
        """
        Fits a model given covariates X as well as option labels y in the
        supervised methods

        Parameters
        ----------
        X : NDArray,(n_samples,n_covariates)
            The data

        y : NDArray,(n_samples,n_prediction)
            Covariates we wish to predict. For now lazy and assuming
            logistic regression.

        task : string,default='classification'
            Is this prediction, multinomial regression, or classification

        progress_bar : bool,default=True
            Whether to print the progress bar to monitor time

        seed : int,default=2021
            The random number generator seed used to ensure reproducibility

        Returns
        -------
        self : PPCADropout
            The model
        """
        self._test_inputs(X, y)
        rng = np.random.default_rng(int(seed))
        training_options = self.training_options
        N, p = X.shape
        self.p = p

        W_, sigmal_ = self._initialize_variables(X)
        X_, y_ = self._transform_training_data(X, 1.0 * y)

        if task == "classification":
            sigm = nn.Sigmoid()
            supervision_loss: nn.BCELoss | nn.MSELoss = nn.BCELoss(
                reduction="mean"
            )

            mod = LogisticRegression(max_iter=1000)
            mod.fit(X, 1.0 * y)
            b_ = torch.tensor(mod.intercept_.astype(np.float32))
        elif task == "regression":
            supervision_loss = nn.MSELoss()
        else:
            err_msg = f"unrecognized_task {task}, must be regression or classification"
            raise ValueError(err_msg)

        trainable_variables = [W_, sigmal_]

        optimizer = torch.optim.SGD(
            trainable_variables,
            lr=training_options["learning_rate"],
            momentum=training_options["momentum"],
        )

        eye = torch.tensor(np.eye(p).astype(np.float32))
        one_s = torch.tensor(np.ones(self.n_supervised).astype(np.float32))
        softplus = nn.Softplus()

        _prior = self._create_prior()

        for i in trange(
            int(training_options["n_iterations"]), disable=not progress_bar
        ):
            idx = rng.choice(
                X_.shape[0], size=training_options["batch_size"], replace=False
            )
            X_batch = X_[idx]
            y_batch = y_[idx]

            sigma = softplus(sigmal_)
            WWT = torch.matmul(torch.transpose(W_, 0, 1), W_)
            Sigma = WWT + sigma * eye

            like_prior = _prior(trainable_variables)

            # Generative likelihood
            m = MultivariateNormal(torch.zeros(p), Sigma)
            like_gen = torch.mean(m.log_prob(X_batch))

            # Predictive lower bound
            P_x, Cov = _get_projection_matrix(W_, sigma)
            mean_z = torch.matmul(X_batch, torch.transpose(P_x, 0, 1))
            eps = torch.rand_like(mean_z)
            C1_2 = torch.linalg.cholesky(Cov)
            z_samples = mean_z + torch.matmul(eps, C1_2)

            if task == "regression":
                y_hat = torch.matmul(z_samples[:, : self.n_supervised], one_s)
                loss_y = supervision_loss(y_hat, y_batch)
            else:
                y_hat = (
                    self.delta
                    * torch.matmul(z_samples[:, : self.n_supervised], one_s)
                    + b_
                )
                loss_y = supervision_loss(sigm(y_hat), y_batch)

            WTW = torch.matmul(W_, torch.transpose(W_, 0, 1))
            off_diag = WTW - torch.diag(torch.diag(WTW))
            loss_i = torch.linalg.matrix_norm(off_diag)

            posterior = like_gen + 1 / N * like_prior
            loss = -1 * posterior + self.mu * loss_y + self.gamma * loss_i

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            self._save_losses(i, like_gen, like_prior, posterior)
            self.losses_supervision[i] = loss_y.detach().numpy()

        self._store_instance_variables(trainable_variables)

        self.B_ = b_.detach().numpy()

        return self

    def _store_instance_variables(self, trainable_variables: list[Tensor]):
        """
        Saves the learned variables

        Parameters
        ----------
        trainable_variables : list
            List of saved variables of type Tensor

        Sets
        ----
        W_ : NDArray,(n_components,p)
            The loadings

        sigma2_ : float
            The isotropic variance

        """
        self.W_ = trainable_variables[0].detach().numpy()
        self.sigma2_ = nn.Softplus()(trainable_variables[1]).detach().numpy()

    def _test_inputs(self, X, y):
        """
        Just tests to make sure data is numpy array and dimensions match
        """
        if not isinstance(X, np.ndarray):
            raise ValueError("Data must be numpy array")
        if self.training_options["batch_size"] > X.shape[0]:
            raise ValueError("Batch size exceeds number of samples")
        if X.shape[0] != len(y):
            err_msg = "Length of data matrix X must equal length of labels y"
            raise ValueError(err_msg)
