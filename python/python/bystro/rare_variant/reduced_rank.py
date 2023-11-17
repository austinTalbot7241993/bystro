"""
This implements sparse reduced rank regression. Traditional approaches like
those found in Wainwright decompose the regression coefficients into a 
low-rank component and a sparse component, with the idea being that most
predictive coefficients for different tasks are similar (the reduced rank
aspect) but a small number of anomolous predictions can be captured by 
the sparse matrix.

Here, however, we assume sparsity and a low rank structure. In other words,
predictive covariates for different diseases should be sparse and similar.
This reflects the idea that for any given disease there are a small number
of relevant genes and that related disorders have related predictors.

Objects
-------
BaseReducedRank(BaseSGDModel, ABC)
    This is the base class of the model. Will never be called directly

Methods
-------
None

"""
from abc import ABC

import numpy as np
from numpy.typing import NDArray
from tqdm import trange

import torch
from torch import Tensor, nn

from bystro._template_sgd_np import BaseSGDModel
from sklearn.linear_model import LogisticRegression  # type: ignore


class BaseReducedRank(BaseSGDModel, ABC):
    """
    This is the base class of the model. Will never be called directly.

    Parameters
    ----------
    n_components : int,default=2
        The latent dimensionality

    Sets
    ----
    creationDate : datetime
        The date/time that the object was created
    """

    def decision_function(self, X: NDArray[np.float_]):
        """
        Predict confidence scores for samples.

        The confidence score for a sample is proportional to the signed
        distance of that sample to the hyperplane.

        Parameters
        ----------
        X : {array-like, sparse matrix} of shape (n_samples, n_features)
            The data matrix for which we want to get the confidence scores.

        Returns
        -------
        scores : ndarray of shape (n_samples,n_classes ) 
            Confidence scores per `(n_samples,n_classes )` combination. 
        """
        Y_hat = np.dot(X, self.B_) + self.alpha_
        return Y_hat

    def predict(self, X: NDArray[np.float_]):
        """
        Predicts Y given learned coefficients B

        Parameters
        ----------
        X : np.array-like,shape=(N,p)
            The predictor variables

        Returns
        -------
        Y_hat : np.array-like,shape=(N,q)
            The predicted values
        """
        scores = self.decision_function(X)
        Y_hat = 1.0 * (scores > 0)
        return Y_hat


class ReducedRankML(BaseReducedRank):
    """
    This fits a reduced rank model using MAP estimation with stochastic
    gradient descent. This is a convex problem so a large batch size 
    is fine for avoiding overfitting, the option is left for stochstic
    merely to accomodate large data sets
    """

    def __init__(
        self,
        lamb_sparsity: float = 1.0,
        lamb_rank: float = 1.0,
        training_options: dict | None = None,
    ):
        """

        Parameters
        ----------
        lamb_sparsity : float,default=1.0
            The sparsity penalty on the coefficients

        lamb_rank : float,default=1.0
            The nuclear norm penalty on the coefficients

        training_options : dict,default={}
            The options for gradient descent

        """
        super().__init__(training_options=training_options)
        self.lamb_sparsity = lamb_sparsity
        self.lamb_rank = lamb_rank

    def fit(
        self,
        X: NDArray[np.float_],
        Y: NDArray[np.float_],
        seed: int = 2021,
        progress_bar: bool = True,
    ):
        """
        Fits a model given covariates X as well as option labels y in the
        supervised methods

        Parameters
        ----------
        X : NDArray,(n_samples,n_covariates)
            The data

        progress_bar : bool,default=True
            Whether to print the progress bar to monitor time

        Returns
        -------
        self : PPCA
            The model
        """
        self._test_inputs(X, Y)
        training_options = self.training_options
        N, p = X.shape
        self.p = p
        _, q = Y.shape
        self.q = q

        rng = np.random.default_rng(int(seed))

        B_, alpha_ = self._initialize_variables(X, Y)
        trainable_variables = [B_, alpha_]
        X_tensor, Y_tensor = self._transform_training_data(X, Y)

        optimizer = torch.optim.SGD(
            trainable_variables,
            lr=training_options["learning_rate"],
            momentum=training_options["momentum"],
        )

        Loss_ce = nn.BCELoss()
        Loss_l1 = nn.L1Loss()
        m = nn.Sigmoid()
        zeros = torch.zeros(p, q)

        for i in trange(
            training_options["n_iterations"], disable=not progress_bar
        ):
            idx = rng.choice(
                X_tensor.shape[0],
                size=training_options["batch_size"],
                replace=False,
            )
            X_batch = X_tensor[idx]
            Y_batch = Y_tensor[idx]

            logits = torch.matmul(X_batch, B_) + alpha_
            probs = m(logits)

            loss_recon = Loss_ce(probs, Y_batch)
            loss_nuc = torch.norm(B_, p="nuc")
            loss_l1 = Loss_l1(B_, zeros)

            loss = (
                loss_recon
                + self.lamb_sparsity * loss_l1
                + self.lamb_rank * loss_nuc
            )

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            self._save_losses(i, loss, loss_recon, loss_l1, loss_nuc)

        self._store_instance_variables(trainable_variables)

        return self

    def _store_instance_variables(self, trainable_variables):
        """
        Saves the learned variables

        Parameters
        ----------
        trainable_variables : list[Tensor]
            List of pytorch variables saved

        Sets
        ----
        B_ : NDArray,(p,q)
            The regression coefficients

        alpha_ : NDArray,(p,q)
            The intercepts
        """
        self.B_ = trainable_variables[0].detach().numpy()
        self.alpha_ = trainable_variables[1].detach().numpy()

    def _initialize_save_losses(self):
        """
        This initializes the arrays to store losses

        Attributes
        ----------
        losses : np.array,size=(td['n_iterations'],)
            Total loss including regularization terms

        losses_recon : np.array,size=(td['n_iterations'],)
            Prediction loss

        losses_nuclear : np.array,size=(td['n_iterations'],)
            The nuclear loss

        losses_sparsity : np.array,size=(td['n_iterations'],)
            The L1 loss 
        """
        n_iterations = self.training_dict["n_iterations"]
        self.losses = np.zeros(n_iterations)
        self.losses_recon = np.zeros(n_iterations)
        self.losses_nuclear = np.zeros(n_iterations)
        self.losses_sparsity = np.zeros(n_iterations)

    def _initialize_variables(
        self, X: NDArray[np.float_], Y: NDArray[np.float_]
    ) -> tuple[Tensor, Tensor]:
        """
        Initializes the variables of the model. Right now fits a PCA model
        in sklearn, uses the loadings and sets sigma^2 to be unexplained
        variance.

        Parameters
        ----------
        X : NDArray,(n_samples,p)
            The explanatory data

        X : NDArray,(n_samples,q)
            The response variables

        Returns
        -------
        B_ : torch.tensor,shape=(p,q)
            The regression coefficients

        alpha_ : torch.tensor,shape=(q,)
            The intercepts
        """
        model = LogisticRegression()
        model.fit(X, Y)
        B_ = torch.tensor(model.coef_, requires_grad=True)
        alpha_ = torch.tensor(model.intercept_, requires_grad=True)
        return B_, alpha_

    def _save_losses(
        self,
        i,
        loss: Tensor,
        loss_recon: Tensor,
        loss_l1: Tensor,
        loss_nuc: Tensor,
    ) -> None:
        """
        Saves the values of the losses at each iteration

        Parameters
        -----------
        i : int
            Current training iteration

        loss : Tensor
            The total loss of the objective 

        loss_l1 : Tensor
            The sparsity loss of the coefficients 

        loss_nuc : Tensor
            The nuclear norm of the coefficients

        loss_recon : Tensor
            The predictive loss
        """
        self.losses[i] = loss.detach().numpy()
        self.losses_recon[i] = loss_recon.detach().numpy()
        self.losses_nuclear[i] = loss_nuc.detach().numpy()
        self.losses_sparsity[i] = loss_l1.detach().numpy()

    def _test_inputs(
        self, X: NDArray[np.float_], Y: NDArray[np.float_]
    ) -> None:
        """
        This tests 
        """
        if not isinstance(X, np.ndarray):
            raise ValueError("Data is numpy array")
        if not isinstance(Y, np.ndarray):
            raise ValueError("Data is numpy array")
        if X.shape[0] != Y.shape[0]:
            raise ValueError("Different observation numbers in X and Y")
        if self.training_options["batch_size"] > X.shape[0]:
            raise ValueError("Batch size exceeds number of samples")

    def _transform_training_data(self, *args: NDArray) -> list[Tensor]:
        """ 
        Convert a list of numpy arrays to tensors
        """
        out = []
        for arg in args:
            out.append(torch.tensor(arg))
        return out
