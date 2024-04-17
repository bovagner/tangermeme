# attribute.py
# Contact: Jacob Schreiber <jmschreiber91@gmail.com>

import torch
import warnings

from tqdm import trange
from .ersatz import dinucleotide_shuffle


def hypothetical_attributions(multipliers, X, references):
	"""A function for aggregating contributions into hypothetical attributions.

	When handling categorical data, like one-hot encodings, the gradients
	returned by a method like DeepLIFT/SHAP may need to be modified because
	the choice of one character at a position explicitly means that the other
	characters are not there. So, one needs to account for each character change 
	actually being the addition of one character AND the subtraction of another 
	character. Basically, once you've calculated the multipliers, you need to 
	subtract out the contribution of the nucleotide actually present and then 
	add in the contribution of the nucleotide you are becomming.

	Each element in the tensor is considered an independent example 

	As an implementation note: to be compatible with Captum, each input must
	be a tuple of length 1 and the returned value will be a tuple of length 1.
	I know this sounds silly but it's the most convenient implementation choice 
	to make the function compatible across DeepLiftShap implementations.


	Parameters
	----------
	multipliers: tuple of one torch.tensor, shape=(n_baselines, 4, length)
		The multipliers/gradient calculated by a method like DeepLIFT/SHAP.
		These should include values for both the observed characters and the
		unobserved characters at each position

	X: tuple of one torch.tensor, shape=(n_baselines, 4, length)
		The one-hot encoded sequence being explained

	references: tuple of one torch.tensor, shape=(n_baselines, 4, length)
		The one-hot encoded reference sequences, usually a shuffled version
		of the corresponding sequence in X.


	Returns
	-------
	projected_contribs: tuple of one torch.tensor, shape=(1, 4, length)
		The attribution values for each nucleotide in the input.
	"""

	for val in multipliers, X, references:
		if not isinstance(val, tuple) or len(val) != 1:
			raise ValueError("All inputs must be one-element tuples.")

		if not isinstance(val[0], torch.Tensor):
			raise ValueError("The first element of each input must be a "
				"tensor.")

		if val[0].shape != multipliers[0].shape:
			raise ValueError("Shape of all tensors must match.") 


	projected_contribs = torch.zeros_like(references[0], dtype=X[0].dtype)
	
	for i in range(X[0].shape[1]):
		hypothetical_input = torch.zeros_like(X[0], dtype=X[0].dtype)
		hypothetical_input[:, i] = 1.0
		hypothetical_diffs = hypothetical_input - references[0]
		hypothetical_contribs = hypothetical_diffs * multipliers[0]

		projected_contribs[:, i] = torch.sum(hypothetical_contribs, dim=1)

	return (projected_contribs,)


class DeepLiftShap():
	"""A vectorized version of the DeepLIFT/SHAP algorithm from Captum.

	DeepLIFT/SHAP is an approach for assigning importance to each input
	feature in an example using principles from game theory. At a high level,
	Shapley values are the average marginal contribution of each feature to
	the prediction and DeepLIFT/SHAP approximates this value for neural
	networks.

	This algorithm is implemented as a class because it is based on the Captum 
	approach of assigning hooks to layers in a PyTorch module object, where the
	hooks modify the gradients to implement the rescale rule. This object 
	implementation is much simpler than the one in Captum and adds in two 
	features: first, the implementation is vectorized so one can accept multiple 
	references for each example and these references can be different across 
	examples and, second, it adds in automatic checks that the theoretical 
	properties of the algorithm hold. 

	IMPORTANT: This implementation is minimal and only supports linear
	operations, convolutions, and dense layers. It does not support any form of
	non-linear pooling operation and may not work on custom operations. I do 
	not know whether it works with transformers. A warning will be raised if 
	the layers are not supported or yield attributions that do not satisfy the
	theoretical properties. Use the _captum_deep_lift_shap function when unsure.


	Parameters
	----------
	model: torch.nn.Module
		A PyTorch model to use for making predictions. These models can take in
		any number of inputs and make any number of outputs. The additional
		inputs must be specified in the `args` parameter.

	ignore_layers: tuple
		A tuple of layer objects that should be ignored when assigning hooks.
		This should be the activations used in the model. Default is
		(torch.nn.ReLU,).

	eps: float, optional
		An epsilon with which to threshold gradients to ensure that there
		isn't an explosion. Default is 1e-6.

	warning_threshold: float, optional
		A threshold on the convergence delta that will always raise a warning
		if the delta is larger than it. Normal deltas are in the range of
		1e-6 to 1e-8. Note that convergence deltas are calculated on the
		gradients prior to the attribution_func being applied to them. Default 
		is 0.001. 

	verbose: bool, optional
		Whether to print the convergence delta for each example that is
		explained, regardless of whether it surpasses the warning threshold.
		Default is False.
	"""

	def __init__(self, model, ignore_layers=(torch.nn.ReLU,), eps=1e-6, 
		warning_threshold=0.001, verbose=False):
		for module in model.named_modules():
			if isinstance(module[1], torch.nn.modules.pooling._MaxPoolNd):
				raise ValueError("Cannot use this implementation of " + 
					"DeepLiftShap with max pooling layers. Please use the " +
					"implementation in Captum.")

		self.model = model
		self.ignore_layers = ignore_layers
		self.eps = eps
		self.warning_threshold = warning_threshold
		self.verbose = verbose

		self.forward_handles = []
		self.backward_handles = []

	def attribute(self, inputs, baselines, args=None):
		"""Run the attribution algorithm on a set of inputs and baselines.

		This method actually handles calculating the attribution values and
		checking to make sure that they follow the theoretical properties of
		attributions.


		Parameters
		----------
		inputs: torch.Tensor, shape=(n, len(alphabet), length)
			A tensor of examples to calculate attribution values for.

		baselines: torch.Tensor, shape=(n, n_baselines, len(alphabet), length)
			A tensor of baselines/references to calculates attributions with
			respect to. The first dimension corresponds to the sequences that
			attributions are calculated for, the second dimension corresponds
			to the number of baselines that are being used for that example,
			the the last two dimensions should match that of the inputs.

		args: tuple, optional
			A tuple of additional forward arguments to pass into the model.
			Even when there is only a single additional argument this must be
			provided as a tuple.


		Returns
		-------
		attributions: torch.Tensor, shape=(n, len(alphabet), length)
			Attributions for each example averaged over all of the baselines
			provided.
		"""

		assert inputs.shape == baselines.shape

		try:
			# Apply hooks and set up inputs
			self.model.apply(self._register_hooks)
			inputs_ = torch.cat([inputs, baselines])

			# Calculate the gradients using the rescale rule
			with torch.autograd.set_grad_enabled(True):
				if args is not None:
					args = (torch.cat([arg, arg]) for arg in 
						args)
					outputs = self.model(inputs_, *args)
				else:
					outputs = self.model(inputs_)

				outputs_ = torch.chunk(outputs, 2)[0].sum()
				gradients = torch.autograd.grad(outputs_, inputs)[0]

			# Check that the prediction-difference-from-reference is equal to
			# the sum of the attributions
			output_diff = torch.sub(*torch.chunk(outputs[:,0], 2))
			input_diff = torch.sum((inputs - baselines) * gradients, dim=(1, 2))
			convergence_deltas = abs(output_diff - input_diff)

			if any(convergence_deltas > self.warning_threshold):
				warnings.warn("Convergence deltas too high: " +   
					str(convergence_deltas))

			if self.verbose:
				print(convergence_deltas)


		finally:
			for forward_handle in self.forward_handles:
				forward_handle.remove()
			for backward_handle in self.backward_handles:
				backward_handle.remove()

		###

		return gradients

	def _forward_pre_hook(self, module, inputs):
		module.input = inputs[0].clone().detach()

	def _forward_hook(self, module, inputs, outputs):
		module.output = outputs.clone().detach()

	def _backward_hook(self, module, grad_input, grad_output):
		delta_in_ = torch.sub(*module.input.chunk(2))
		delta_out_ = torch.sub(*module.output.chunk(2))

		delta_in = torch.cat([delta_in_, delta_in_])
		delta_out = torch.cat([delta_out_, delta_out_])

		delta = delta_out / delta_in

		grad_input = (torch.where(
			abs(delta_in) < self.eps, grad_input[0], grad_output[0] * delta),
		)
		return grad_input

	def _can_register_hook(self, module):
		if len(module._backward_hooks) > 0:
			return False
		if not isinstance(module, self.ignore_layers):
			return False
		return True

	def _register_hooks(self, module, attribute_to_layer_input=True):
		if not self._can_register_hook(module) or (
			not attribute_to_layer_input and module is self.layer
		):
			return

		# adds forward hook to leaf nodes that are non-linear
		forward_handle = module.register_forward_hook(self._forward_hook)
		pre_forward_handle = module.register_forward_pre_hook(
			self._forward_pre_hook)
		backward_handle = module.register_full_backward_hook(
			self._backward_hook)

		self.forward_handles.append(forward_handle)
		self.forward_handles.append(pre_forward_handle)
		self.backward_handles.append(backward_handle)


def deep_lift_shap(model, X, args=None, references=dinucleotide_shuffle, 
	n_shuffles=20, batch_size=32, return_references=False, hypothetical=False,
	warning_threshold=0.001, print_convergence_deltas=False, device='cuda', 
	random_state=None, verbose=False):
	"""Calculate attributions using DeepLift/Shap and a given model. 

	This function will calculate DeepLift/Shap attributions on a set of
	sequences. It assumes that the model returns "logits" in the first output,
	not softmax probabilities, and count predictions in the second output.
	It will create GC-matched negatives to use as a reference and proceed
	using the given batch size.


	Parameters
	----------
	model: torch.nn.Module
		A PyTorch model to use for making predictions. These models can take in
		any number of inputs and make any number of outputs. The additional
		inputs must be specified in the `args` parameter.

	X: torch.tensor, shape=(-1, len(alphabet), length)
		A set of one-hot encoded sequences to calculate attribution values
		for. 

	args: tuple or None, optional
		An optional set of additional arguments to pass into the model. If
		provided, each element in the tuple or list is one input to the model
		and the element must be formatted to be the same batch size as `X`. If
		None, no additional arguments are passed into the forward function.
		Default is None.

	references: func or torch.Tensor, optional
		If a function is passed in, this function is applied to each sequence
		with the provided random state and number of shuffles. This function
		should serve to transform a sequence into some form of signal-null
		background, such as by shuffling it. If a torch.Tensor is passed in,
		that tensor must have shape `(len(X), n_shuffles, *X.shape[1:])`, in
		that for each sequence a number of shuffles are provided. Default is
		the function `dinucleotide_shuffle`. 

	n_shuffles: int, optional
		The number of shuffles to use if a function is given for `references`.
		If a torch.Tensor is provided, this number is ignored. Default is 20.

	batch_size: int, optional
		The number of sequence-reference pairs to pass through DeepLiftShap at
		a time. Importantly, this is not the number of elements in `X` that
		are processed simultaneously (alongside ALL their references) but the
		total number of `X`-`reference` pairs that are processed. This means
		that if you are in a memory-limited setting where you cannot process
		all references for even a single sequence simultaneously that the
		work is broken down into doing only a few references at a time. Default
		is 32.

	return_references: bool, optional
		Whether to return the references that were generated during this
		process. Only use if `references` is not a torch.Tensor. Default is 
		False. 

	hypothetical: bool, optional
		Whether to return attributions for all possible characters at each
		position or only for the character that is actually at the sequence.
		Practically, whether to return the returned attributions from captum
		with the one-hot encoded sequence. Default is False.

	warning_threshold: float, optional
		A threshold on the convergence delta that will always raise a warning
		if the delta is larger than it. Normal deltas are in the range of
		1e-6 to 1e-8. Note that convergence deltas are calculated on the
		gradients prior to the aggr_func being applied to them. Default 
		is 0.001. 

	print_convergence_deltas: bool, optional
		Whether to print the convergence deltas for each example when using
		DeepLiftShap. Default is False.


	device: str or torch.device, optional
		The device to move the model and batches to when making predictions. If
		set to 'cuda' without a GPU, this function will crash and must be set
		to 'cpu'. Default is 'cuda'. 

	random_state: int or None or numpy.random.RandomState, optional
		The random seed to use to ensure determinism. If None, the
		process is not deterministic. Default is None. 

	verbose: bool, optional
		Whether to display a progress bar. Default is False.


	Returns
	-------
	attributions: torch.tensor
		The attributions calculated for each input sequence, with the same
		shape as the input sequences.

	references: torch.tensor, optional
		The references used for each input sequence, with the shape
		(n_input_sequences, n_shuffles, 4, length). Only returned if
		`return_references = True`. 
	"""

	attributions, references_ = [], []
	model = model.to(device)

	if isinstance(references, torch.Tensor):
		n_shuffles = references.shape[1]

	n = X.shape[0] * n_shuffles
	Xi, rj, attr_ = [], [], []
	z = 0

	for i in trange(n, disable=not verbose):
		Xi.append(i // n_shuffles)
		rj.append(i % n_shuffles)

		if len(Xi) == batch_size or i == (n-1):
			_X = X[Xi].to(device).requires_grad_()
			_args = None if args is None else tuple([a[Xi].to(device) 
				for a in args])

			# Handle reference sequences while ensuring that the same seed is
			# used for each shuffle even if not all shuffles are done in the
			# same batch.
			if isinstance(references, torch.Tensor):
				_references = references[Xi, rj]
			else:
				if random_state is None:
					_references = references(_X, n=1)[:, 0]
				else:
					_references = torch.cat([references(_X[j:j+1], n=1, 
						random_state=random_state+rj[j])[:, 0] 
							for j in range(len(_X))])

			_references = _references.requires_grad_().to(device)

			# Run DeepLiftShap
			gradients = DeepLiftShap(model, warning_threshold=warning_threshold, 
				verbose=print_convergence_deltas).attribute(_X, _references, 
				args=_args)
			
			attr = hypothetical_attributions((gradients,), (_X,), 
				(_references,))[0]
			attr_.extend(list(attr))

			# Average across all references for each example
			while len(attr_) >= n_shuffles:
				attr_avg = torch.stack(attr_[:n_shuffles]).mean(dim=0)
				if not hypothetical:
					attr_avg *= X[z].to(device)

				attributions.append(attr_avg.cpu().detach())
				attr_ = attr_[n_shuffles:]
				z += 1

			if return_references:
				references_.extend(list(_references.cpu().detach()))

			Xi, rj = [], []

	attributions = torch.stack(attributions)
	
	if return_references:
		references_ = torch.cat(references_).reshape(X.shape[0], n_shuffles, 
			*X.shape[1:])
		return attributions, references_
	
	return attributions


def _captum_deep_lift_shap(model, X, args=None, references=dinucleotide_shuffle, 
	n_shuffles=20, batch_size=32, return_references=False, hypothetical=False,
	device='cuda', random_state=None, verbose=False, ):
	"""Calculate attributions using DeepLift/Shap and a given model. 

	This function will calculate DeepLift/Shap attributions on a set of
	sequences. It assumes that the model returns "logits" in the first output,
	not softmax probabilities, and count predictions in the second output.
	It will create GC-matched negatives to use as a reference and proceed
	using the given batch size.

	This is an internal/debugging function that is mostly meant to be used to
	check for differences with the `deep_lift_shap` method.


	Parameters
	----------
	model: torch.nn.Module
		A PyTorch model to use for making predictions. These models can take in
		any number of inputs and make any number of outputs. The additional
		inputs must be specified in the `args` parameter.

	X: torch.tensor, shape=(-1, len(alphabet), length)
		A set of one-hot encoded sequences to calculate attribution values
		for. 

	args: tuple or None, optional
		An optional set of additional arguments to pass into the model. If
		provided, each element in the tuple or list is one input to the model
		and the element must be formatted to be the same batch size as `X`. If
		None, no additional arguments are passed into the forward function.
		Default is None.

	references: func or torch.Tensor, optional
		If a function is passed in, this function is applied to each sequence
		with the provided random state and number of shuffles. This function
		should serve to transform a sequence into some form of signal-null
		background, such as by shuffling it. If a torch.Tensor is passed in,
		that tensor must have shape `(len(X), n_shuffles, *X.shape[1:])`, in
		that for each sequence a number of shuffles are provided. Default is
		the function `dinucleotide_shuffle`. 

	n_shuffles: int, optional
		The number of shuffles to use if a function is given for `references`.
		If a torch.Tensor is provided, this number is ignored. Default is 20.

	batch_size: int, optional
		The number of sequence-reference pairs to pass through DeepLiftShap at
		a time. Importantly, this is not the number of elements in `X` that
		are processed simultaneously (alongside ALL their references) but the
		total number of `X`-`reference` pairs that are processed. This means
		that if you are in a memory-limited setting where you cannot process
		all references for even a single sequence simultaneously that the
		work is broken down into doing only a few references at a time. Default
		is 32.

	return_references: bool, optional
		Whether to return the references that were generated during this
		process. Only use if `references` is not a torch.Tensor. Default is 
		False. 

	hypothetical: bool, optional
		Whether to return attributions for all possible characters at each
		position or only for the character that is actually at the sequence.
		Practically, whether to return the returned attributions from captum
		with the one-hot encoded sequence. Default is False.

	device: str or torch.device, optional
		The device to move the model and batches to when making predictions. If
		set to 'cuda' without a GPU, this function will crash and must be set
		to 'cpu'. Default is 'cuda'. 

	random_state: int or None or numpy.random.RandomState, optional
		The random seed to use to ensure determinism. If None, the
		process is not deterministic. Default is None. 

	verbose: bool, optional
		Whether to display a progress bar. Default is False.


	Returns
	-------
	attributions: torch.tensor
		The attributions calculated for each input sequence, with the same
		shape as the input sequences.

	references: torch.tensor, optional
		The references used for each input sequence, with the shape
		(n_input_sequences, n_shuffles, 4, length). Only returned if
		`return_references = True`. 
	"""

	from captum.attr import DeepLiftShap

	attributions = []
	references_ = []
	with torch.no_grad():
		for i in trange(len(X), disable=not verbose):
			ig = DeepLiftShap(model)

			_X = X[i:i+1].to(device)
			_args = None if args is None else tuple([a[i:i+1].to(device) 
				for a in args])

			# Calculate references
			if isinstance(references, torch.Tensor):
				_references = references[i:i+1].to(device)[0]
			else:
				_references = references(_X, n=n_shuffles, 
					random_state=random_state)[0].to(device)
						
			attr = ig.attribute(_X, _references, target=0, 
				additional_forward_args=_args, 
				custom_attribution_func=hypothetical_attributions)

			if not hypothetical:
				attr = (attr * _X)
			
			if return_references:
				references_.append(_reference.unsqueeze(0))

			attributions.append(attr.cpu())

	attributions = torch.cat(attributions)

	if return_references:
		return attributions, torch.cat(references_)
	return attributions