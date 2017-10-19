import os
import hickle
import lasagne
import numpy as np
import theano
import theano.tensor as T

class SSTSequenceEncoder(object):
    """Class that encapsulates the sequence encoder and the output proposals
    stages of the SST (Single-Stream Temporal Action Proposals) model.
    """
    def __init__(self, input_var=None, target_var=None, seq_length=None, num_proposals=16,
            depth=1, width=256, input_size=500, grad_clip=100, reset_bias=5.0,
            dropout=0, mode='train', w0=None, w1=None, verbose=False, **kwargs):
        """Initialize the model architecture. See Section 3 in the main paper
        for additional details.

        Parameters
        ----------
        input_var : theano variable, optional
            This is where you can link up an existing Theano graph
            of a visual encoder as input to the sequence encoder and
            output stages.
        seq_length : int, optional
            Use this if you wish to hard-code a specified sequence length for
            the SST model. If 'None' then model will run on arbitrary/untrimmed
            sequence input.
        num_proposals : int, optional
            The number of proposal anchors that should be considered at each
            time step of the model.
        depth : int, optional
            Number of recurrent layers in the sequence encoder.
        width : int, optional
            Size of hidden state in each recurrent layer.
        input_size : int, optional
            Size of the input feature encodings (size of output of vis. encoder)
        dropout : float, optional
            Will add dropout layers for regularization if p > 0.
        grad_clip, reset_bias : optional
            Parameters that are only needed for training. For the purpose of
            evaluation from pre-trained params, these are ignored/overwritten.
        verbose : bool, optional
            Optional flag to enable verbose print statements.

        Raises
        ------
        ValueError
            Invalid value for dropout or num_proposals
        """
        self.mode = mode
        self.input_var = input_var
        self.target_var = target_var
        self.w0 = w0
        self.w1 = w1
        self.seq_length = seq_length
        self.num_proposals = num_proposals
        if num_proposals < 0.0:
            raise ValueError(("Must provide positive number of proposal"
                              "anchors. (Provided: {})").format(num_proposals))
        self.depth = depth
        self.width = width
        self.input_size = input_size
        self.grad_clip = grad_clip
        self.reset_bias = reset_bias
        if dropout < 0.0 or dropout > 1.0:
            raise ValueError("Invalid value for dropout (p={})".format(dropout))
        elif dropout > 0 and verbose:
            print("Enabled dropout with probability p = {}".format(dropout))
        self.dropout = dropout
        self.verbose = verbose
        self.train_fn = None
        self.test_fn = None
        self._network = self._build_network() # retains theano symbolic graph

    def _build_network(self):
        """Build the theano graph of the model architecture.
        """
        # input layer
        input_shape = (None, self.seq_length, self.input_size)
        l_input = lasagne.layers.InputLayer(shape=input_shape,
                                            input_var=self.input_var)
        # obtain symbolic references for later
        batchsize, seqlen, _ = l_input.input_var.shape
        l_gru = l_input # needed for for-loop below
        dropout_enabled = self.dropout > 0
        # recurrent layers
        for _ in range(self.depth):
            l_gru = lasagne.layers.GRULayer(
                l_gru, self.width, grad_clipping=self.grad_clip)

            #Bi-directional layer
            #l_gru_back = lasagne.layers.GRULayer(
            #    l_gru, self.width, grad_clipping=self.grad_clip, backwards=True)
            #l_gru = lasagne.layers.concat((l_gru, l_gru_back), axis=2)

            if dropout_enabled: # add dropout!
                l_gru = lasagne.layers.DropoutLayer(l_gru, p=self.dropout)
        # reshape -> dense layer (sigmoid) -> reshape back for outputs.
        l_reshape = lasagne.layers.ReshapeLayer(l_gru, (-1, self.width))
        nonlin_out = lasagne.nonlinearities.sigmoid
        l_dense = lasagne.layers.DenseLayer(l_reshape,
                num_units=self.num_proposals,
                nonlinearity=nonlin_out)
        final_output_shape = (batchsize, seqlen, self.num_proposals)
        l_out = lasagne.layers.ReshapeLayer(l_dense, final_output_shape)
        return l_out # retain theano representation of model graph

    def initialize_pretrained(self, model_params, **kwargs):
        """Initialize model parameters to pre-trained weights (model_params).
        """
        if not callable(self.test_fn):
            lasagne.layers.set_all_param_values(self._network, model_params)
        elif self.verbose:
            print("Model is already compiled! Ignoring provided model_params.")
        return self

    def compile(self, **kwargs):
        """Compiles model for evaluation.
        """
        if not callable(self.test_fn):
            if self.mode == 'train':
                #Train function
                term1 = self.w1 * self.target_var
                term2 = self.w0 * (1.0 - self.target_var)
                train_prediction = T.clip(lasagne.layers.get_output(self._network), 0.001, 0.999)
                #loss = lasagne.objectives.binary_crossentropy(train_prediction.ravel(), self.target_var.ravel())
                loss = -(term1.ravel() * T.log(train_prediction.ravel()) + (1.0 - term2.ravel()) * T.log(1.0 - train_prediction.ravel()))
                loss = loss.mean()
                params = lasagne.layers.get_all_params(self._network, trainable=True)
                updates = lasagne.updates.nesterov_momentum(
                        loss, params, learning_rate=0.01, momentum=0.9)
                #updates = lasagne.updates.adam(loss, params, learning_rate = 0.05)
                self.train_fn = theano.function([self.input_var, self.target_var], loss, updates=updates)
                #Test function
                test_prediction = T.clip(lasagne.layers.get_output(self._network,
                                                        deterministic=True), 0.001, 0.999)
                #test_loss = lasagne.objectives.binary_crossentropy(test_prediction.ravel(), self.target_var.ravel())
                term1 = self.w1 * self.target_var
                term2 = self.w0 * (1.0 - self.target_var)
                test_loss = -(term1.ravel() * T.log(train_prediction.ravel()) + (1.0 - term2.ravel()) * T.log(1.0 - train_prediction.ravel()))
                test_loss = test_loss.mean()
                test_fn = theano.function([self.input_var, self.target_var], test_loss)
            elif self.mode == 'test':
                test_prediction = lasagne.layers.get_output(self._network,
                    deterministic=True)
                test_fn = theano.function([self.input_var], test_prediction)
            self.test_fn = test_fn
        elif self.verbose:
            print("Model is already compiled - skipping compilation operation.")
        return self

    def forward_eval(self, input_data, input_labels=None):
        """ Performs forward pass to obtain predicted confidence scores over
        the discretized input video stream(s).

        Parameters
        ----------
        input_data : ndarray
            Must be three dimensional, where first dimension is the number of
            input video stream(s), the second is the number of time steps, and
            the third is the size of the visual encoder output for each time
            step. Shape of tensor = (n_vids, L, input_size).

        Returns
        -------
        y_pred : ndarray
            Two-dimensional ndarray of size (n_vids, L, K), where L is the
            number of time steps (length of input discretized video), and K is
            the number of proposal anchors at each time step (num_proposals).

        Raises
        ------
        ValueError
            If model has not been compiled or input data is malformed.
        """
        if not callable(self.test_fn):
            raise ValueError("Model must be compiled.")
        if input_data.ndim != 3:
            raise ValueError("Input ndarray must be three dimensional.")
        if input_data.shape[2] != self.input_size:
            raise ValueError(("Mismatch between input visual encoding size and network input size."))

        if self.mode == 'train':
            output = self.train_fn(input_data, input_labels)
            output = self.test_fn(input_data, input_labels)
        elif self.mode == 'test':
            output = self.test_fn(input_data)
        return output

    def load_model_params(self, filename):
        """Unpickles and loads parameters into a Lasagne model."""
        with open(filename, 'r') as f:
            data = hickle.load(f)
            #data = data['params']
        lasagne.layers.set_all_param_values(self._network, data)

    def save_model_params(self, filename):
        """Pickels the parameters within a Lasagne model."""
        data = lasagne.layers.get_all_param_values(self._network)
        filename = os.path.join('./', filename)
        with open(filename, 'w') as f:
            hickle.dump(data, f)
