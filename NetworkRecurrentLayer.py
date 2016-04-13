
import numpy
from theano import tensor as T
import theano
from NetworkHiddenLayer import HiddenLayer
from NetworkBaseLayer import Container, Layer
from ActivationFunctions import strtoact
from math import sqrt
from OpLSTM import LSTMOpInstance
import RecurrentTransform
import json

class Unit(Container):
  """
  Abstract descriptor class for all kinds of recurrent units.
  """
  def __init__(self, n_units, n_in, n_out, n_re, n_act):
    """
    :param n_units: number of cells
    :param n_in: cell fan in
    :param n_out: cell fan out
    :param n_re: recurrent fan in
    :param n_act: number of outputs
    """
    self.n_units, self.n_in, self.n_out, self.n_re, self.n_act = n_units, n_in, n_out, n_re, n_act
    self.slice = T.constant(self.n_units, dtype='int32')
    self.params = {}

  def set_parent(self, parent):
    """
    :type parent: RecurrentUnitLayer
    """
    self.parent = parent

  def scan(self, x, z, non_sequences, i, outputs_info, W_re, W_in, b, go_backwards=False, truncate_gradient=-1):
    """
    Executes the iteration over the time axis (usually with theano.scan)
    :param step: python function to be executed
    :param x: unmapped input tensor in (time,batch,dim) shape
    :param z: same as x but already transformed to self.n_in
    :param non_sequences: see theano.scan
    :param i: index vector in (time, batch) shape
    :param outputs_info: see theano.scan
    :param W_re: recurrent weight matrix
    :param W_in: input weight matrix
    :param b: input bias
    :param go_backwards: whether to scan the sequence from 0 to T or from T to 0
    :param truncate_gradient: see theano.scan
    :return:
    """
    self.outputs_info = outputs_info
    self.non_sequences = non_sequences
    self.W_re = W_re
    self.W_in = W_in
    self.b = b
    self.go_backwards = go_backwards
    self.truncate_gradient = truncate_gradient
    try:
      self.xc = z if not x else T.concatenate([s.output for s in x], axis = -1)
    except Exception:
      self.xc = z if not x else T.concatenate(x, axis = -1)

    outputs, _ = theano.scan(self.step,
                             #strict = True,
                             truncate_gradient = truncate_gradient,
                             go_backwards = go_backwards,
                             sequences = [i,self.xc,z],
                             non_sequences = non_sequences,
                             outputs_info = outputs_info)
    return outputs


class VANILLA(Unit):
  """
  A simple tanh unit
  """
  def __init__(self, n_units,  **kwargs):
    super(VANILLA, self).__init__(n_units, n_units, n_units, n_units, 1)

  def step(self, i_t, x_t, z_t, z_p, h_p):
    """
    performs one iteration of the recursion
    :param i_t: index at time step t
    :param x_t: raw input at time step t
    :param z_t: mapped input at time step t
    :param z_p: previous input from time step t-1
    :param h_p: previous hidden activation from time step t-1
    :return:
    """
    return [ T.tanh(z_t + z_p) ]


class LSTME(Unit):
  """
  A theano based LSTM implementation
  """
  def __init__(self, n_units, **kwargs):
    super(LSTME, self).__init__(
      n_units=n_units,
      n_in=n_units * 4,  # input gate, forget gate, output gate, net input
      n_out=n_units,
      n_re=n_units * 4,
      n_act=2  # output, cell state
    )
    self.o_output = T.as_tensor(numpy.ones((n_units,), dtype='float32'))
    self.o_h = T.as_tensor(numpy.ones((n_units,), dtype='float32'))

  def step(self, i_t, x_t, z_t, y_p, c_p, *other_args):
    # See Unit.scan() for seqs.
    # args: seqs (x_t = unit.xc, z_t, i_t), outputs (# unit.n_act, y_p, c_p, ...), non_seqs (none)
    other_outputs = []
    if self.recurrent_transform:
      state_vars = other_args[:len(self.recurrent_transform.state_vars)]
      self.recurrent_transform.set_sorted_state_vars(state_vars)
      z_r, r_updates = self.recurrent_transform.step(y_p)
      z_t += z_r
      for v in self.recurrent_transform.get_sorted_state_vars():
        other_outputs += [r_updates[v]]
    z_t += T.dot(y_p, self.W_re)
    partition = z_t.shape[1] / 4
    ingate = T.nnet.sigmoid(z_t[:,:partition])
    forgetgate = T.nnet.sigmoid(z_t[:,partition:2*partition])
    outgate = T.nnet.sigmoid(z_t[:,2*partition:3*partition])
    input = T.tanh(z_t[:,3*partition:4*partition])
    c_t = forgetgate * c_p + ingate * input
    y_t = outgate * T.tanh(c_t)
    i_output = T.outer(i_t, self.o_output)
    i_h = T.outer(i_t, self.o_h)
    # return: next outputs (# unit.n_act, y_t, c_t, ...)
    return (y_t * i_output, c_t * i_h + c_p * (1 - i_h)) + tuple(other_outputs)


class LSTMP(Unit):
  """
  Very fast custom LSTM implementation
  """
  def __init__(self, n_units, **kwargs):
    super(LSTMP, self).__init__(n_units, n_units * 4, n_units, n_units * 4, 2)

  def scan(self, x, z, non_sequences, i, outputs_info, W_re, W_in, b, go_backwards = False, truncate_gradient = -1):
    z = T.inc_subtensor(z[-1 if go_backwards else 0], T.dot(outputs_info[0],W_re))
    result = LSTMOpInstance(z[::-(2 * go_backwards - 1)], W_re, outputs_info[1], i[::-(2 * go_backwards - 1)])
    return [ result[0], result[2].dimshuffle('x',0,1) ]


class LSTMC(Unit):
  """
  The same implementation as above, but it executes a theano function (recurrent transform)
  in each iteration. This allows for additional dependencies in the recursion of the LSTM.
  """
  def __init__(self, n_units, **kwargs):
    super(LSTMC, self).__init__(n_units, n_units * 4, n_units, n_units * 4, 2)

  def scan(self, x, z, non_sequences, i, outputs_info, W_re, W_in, b, go_backwards = False, truncate_gradient = -1):
    assert self.parent.recurrent_transform
    import OpLSTMCustom
    op = OpLSTMCustom.register_func(self.parent.recurrent_transform)
    custom_vars = self.parent.recurrent_transform.get_sorted_custom_vars()
    initial_state_vars = self.parent.recurrent_transform.get_sorted_state_vars_initial()
    # See OpLSTMCustom.LSTMCustomOp.
    # Inputs args are: Z, c, y0, i, W_re, custom input vars, initial state vars
    # Results: (output) Y, (gates and cell state) H, (final cell state) d, state vars sequences
    op_res = op(z[::-(2 * go_backwards - 1)],
                outputs_info[1], outputs_info[0], i[::-(2 * go_backwards - 1)], W_re, *(custom_vars + initial_state_vars))
    result = [ op_res[0], op_res[2].dimshuffle('x',0,1) ] + op_res[3:]
    assert len(result) == len(outputs_info)
    return result


class GRU(Unit):
  """
  Gated recurrent unit as described in http://arxiv.org/abs/1502.02367
  """
  def __init__(self, n_units, **kwargs):
    super(GRU, self).__init__(n_units, n_units * 3, n_units, n_units * 2, 1)
    l = sqrt(6.) / sqrt(n_units * 3)
    rng = numpy.random.RandomState(1234)
    values = numpy.asarray(rng.uniform(low=-l, high=l, size=(n_units, n_units)), dtype=theano.config.floatX)
    self.W_reset = theano.shared(value=values, borrow=True, name = "W_reset")
    self.params['W_reset'] = self.W_reset

  def step(self, i_t, x_t, z_t, z_p, h_p):
    CI, GR, GU = [T.tanh, T.nnet.sigmoid, T.nnet.sigmoid]
    u_t = GU(z_t[:,:self.slice] + z_p[:,:self.slice])
    r_t = GR(z_t[:,self.slice:2*self.slice] + z_p[:,self.slice:2*self.slice])
    h_c = CI(z_t[:,2*self.slice:] + self.dot(r_t * h_p, self.W_reset))
    return u_t * h_p + (1 - u_t) * h_c


class SRU(Unit):
  """
  Same as GRU but without reset weights, which allows for a faster computation on GPUs
  """
  def __init__(self, n_units, **kwargs):
    super(SRU, self).__init__(n_units, n_units * 3, n_units, n_units * 3, 1)

  def step(self, i_t, x_t, z_t, z_p, h_p):
    CI, GR, GU = [T.tanh, T.nnet.sigmoid, T.nnet.sigmoid]
    u_t = GU(z_t[:,:self.slice] + z_p[:,:self.slice])
    r_t = GR(z_t[:,self.slice:2*self.slice] + z_p[:,self.slice:2*self.slice])
    h_c = CI(z_t[:,2*self.slice:3*self.slice] + r_t * z_p[:,2*self.slice:3*self.slice])
    return  u_t * h_p + (1 - u_t) * h_c


class RecurrentUnitLayer(Layer):
  """
  Layer class to execute recurrent units
  """
  recurrent = True
  layer_class = "rec"

  def __init__(self,
               n_out,
               n_units = None,
               direction = 1,
               truncation = -1,
               sampling = 1,
               encoder = None,
               unit = 'lstm',
               n_dec = 0,
               attention = "none",
               recurrent_transform = "none",
               recurrent_transform_attribs = "{}",
               attention_template = None,
               attention_distance = 'l2',
               attention_step = "linear",
               attention_beam = 0,
               attention_norm = "exp",
               attention_momentum = "none",
               attention_sharpening = 1.0,
               attention_nbest = 0,
               attention_store = False,
               attention_align = False,
               attention_glimpse = 1,
               attention_bn = 0,
               attention_lm = 'none',
               base = None,
               lm = False,
               force_lm = False,
               droplm = 1.0,
               bias_random_init_forget_shift=0.0,
               **kwargs):
    """
    :param n_out: number of cells
    :param n_units: used when initialized via Network.from_hdf_model_topology
    :param direction: process sequence in forward (1) or backward (-1) direction
    :param truncation: gradient truncation
    :param sampling: scan every nth frame only
    :param encoder: list of encoder layers used as initalization for the hidden state
    :param unit: cell type (one of 'lstm', 'vanilla', 'gru', 'sru')
    :param n_dec: absolute number of steps to unfold the network if integer, else relative number of steps from encoder
    :param recurrent_transform: name of recurrent transform
    :param recurrent_transform_attribs: dictionary containing parameters for a recurrent transform
    :param attention_template:
    :param attention_distance:
    :param attention_step:
    :param attention_beam:
    :param attention_norm:
    :param attention_sharpening:
    :param attention_nbest:
    :param attention_store:
    :param attention_align:
    :param attention_glimpse:
    :param attention_lm:
    :param base: list of layers which outputs are considered as based during attention mechanisms
    :param lm: activate RNNLM
    :param force_lm: expect previous labels to be given during testing
    :param droplm: probability to take the expected output as predecessor instead of the real one when LM=true
    :param bias_random_init_forget_shift: initialize forget gate bias of lstm networks with this value
    """
    unit_given = unit
    if unit == 'lstm':  # auto selection
      if str(theano.config.device).startswith('cpu'):
        unit = 'lstme'
      elif recurrent_transform == 'none' and (not lm or droplm == 0.0):
        unit = 'lstmp'
      else:
        unit = 'lstmc'
    elif unit in ("lstmc", "lstmp") and str(theano.config.device).startswith('cpu'):
      unit = "lstme"
    kwargs.setdefault("n_out", n_out)
    if n_units is not None:
      assert n_units == n_out
    self.attention_weight = T.constant(1.,'float32')
    if len(kwargs['sources']) == 1 and kwargs['sources'][0].layer_class.startswith('length'):
      kwargs['sources'] = []
    elif len(kwargs['sources']) == 1 and kwargs['sources'][0].layer_class.startswith('signal'):
      #self.attention_weight = kwargs['sources'][0].xflag
      kwargs['sources'] = []
    super(RecurrentUnitLayer, self).__init__(**kwargs)
    self.set_attr('from', ",".join([s.name for s in self.sources]) if self.sources else "null")
    self.set_attr('n_out', n_out)
    self.set_attr('unit', unit_given.encode("utf8"))
    self.set_attr('truncation', truncation)
    self.set_attr('sampling', sampling)
    self.set_attr('direction', direction)
    self.set_attr('lm', lm)
    self.set_attr('force_lm', force_lm)
    self.set_attr('droplm', droplm)
    if bias_random_init_forget_shift:
      self.set_attr("bias_random_init_forget_shift", bias_random_init_forget_shift)
    self.set_attr('attention_beam', attention_beam)
    self.set_attr('recurrent_transform', recurrent_transform.encode("utf8"))
    if isinstance(recurrent_transform_attribs, str):
      recurrent_transform_attribs = json.loads(recurrent_transform_attribs)
    if attention_template is not None:
      self.set_attr('attention_template', attention_template)
    self.set_attr('recurrent_transform_attribs', recurrent_transform_attribs)
    self.set_attr('attention_distance', attention_distance.encode("utf8"))
    self.set_attr('attention_step', attention_step.encode("utf8"))
    self.set_attr('attention_norm', attention_norm.encode("utf8"))
    self.set_attr('attention_sharpening', attention_sharpening)
    self.set_attr('attention_nbest', attention_nbest)
    attention_store = attention_store or attention_momentum
    self.set_attr('attention_store', attention_store)
    self.set_attr('attention_align', attention_align)
    self.set_attr('attention_glimpse', attention_glimpse)
    self.set_attr('attention_lm', attention_lm)
    self.set_attr('attention_bn', attention_bn)
    self.set_attr('n_dec', n_dec)
    if encoder:
      self.set_attr('encoder', ",".join([e.name for e in encoder]))
    if base:
      self.set_attr('base', ",".join([b.name for b in base]))
    else:
      base = encoder
    self.base = base
    self.set_attr('n_units', n_out)
    unit = eval(unit.upper())(**self.attrs)
    assert isinstance(unit, Unit)
    self.unit = unit
    kwargs.setdefault("n_out", unit.n_out)
    if n_dec != 0:
      self.target_index = self.index
      if isinstance(n_dec,float):
        lengths = T.cast(T.ceil(T.sum(T.cast(encoder[0].index,'float32'),axis=0) * n_dec), 'int32')
        if n_dec <= 1.0:
          idx, _ = theano.map(lambda l_i, l_m:T.concatenate([T.ones((l_i,),'int8'),T.zeros((l_m-l_i,),'int8')]),
                              [lengths], [T.max(lengths)+1])
          self.index = idx.dimshuffle(1,0)[:-1]
        else:
          self.index = T.alloc(numpy.cast[numpy.int8](1), n_dec, self.index.shape[1])
        n_dec = T.cast(T.ceil(T.cast(encoder[0].index.shape[0],'float32') * numpy.float32(n_dec)),'int32')
    else:
      n_dec = self.index.shape[0]
    # initialize recurrent weights
    W_re = None
    if unit.n_re > 0:
      W_re = self.add_param(self.create_random_uniform_weights(unit.n_out, unit.n_re, name="W_re_%s" % self.name))
    # initialize forward weights
    bias_init_value = numpy.zeros((unit.n_in, ), dtype = theano.config.floatX)
    if bias_random_init_forget_shift:
      assert unit.n_units * 4 == unit.n_in  # (input gate, forget gate, output gate, net input)
      bias_init_value[unit.n_units:2 * unit.n_units] += bias_random_init_forget_shift
    self.b.set_value(bias_init_value)
    self.W_in = []
    for s in self.sources:
      W = self.create_random_uniform_weights(s.attrs['n_out'], unit.n_in,
                                             s.attrs['n_out'] + unit.n_in + unit.n_re,
                                             name="W_in_%s_%s" % (s.name, self.name))
      self.W_in.append(self.add_param(W))
    # make input
    z = self.b
    for x_t, m, W in zip(self.sources, self.masks, self.W_in):
      if x_t.attrs['sparse']:
        if x_t.output.ndim == 3: out_dim = x_t.output.shape[2]
        elif x_t.output.ndim == 2: out_dim = 1
        else: assert False, x_t.output.ndim
        if x_t.output.ndim == 3:
          z += W[T.cast(x_t.output[:,:,0], 'int32')]
        elif x_t.output.ndim == 2:
          z += W[T.cast(x_t.output, 'int32')]
        else:
          assert False, x_t.output.ndim
      elif m is None:
        z += T.dot(x_t.output, W)
      else:
        z += self.dot(self.mass * m * x_t.output, W)
    num_batches = self.index.shape[1]
    self.num_batches = num_batches
    non_sequences = []
    if self.attrs['lm']:
      if not 'target' in self.attrs:
        self.attrs['target'] = 'classes'
      l = sqrt(6.) / sqrt(unit.n_out + self.y_in[self.attrs['target']].n_out)
      values = numpy.asarray(self.rng.uniform(low=-l, high=l, size=(unit.n_out, self.y_in[self.attrs['target']].n_out)), dtype=theano.config.floatX)
      self.W_lm_in = self.add_param(self.shared(value=values, borrow=True, name = "W_lm_in_"+self.name))
      l = sqrt(6.) / sqrt(unit.n_in + self.y_in[self.attrs['target']].n_out)
      values = numpy.asarray(self.rng.uniform(low=-l, high=l, size=(self.y_in[self.attrs['target']].n_out, unit.n_in)), dtype=theano.config.floatX)
      self.W_lm_out = self.add_param(self.shared(value=values, borrow=True, name = "W_lm_out_"+self.name))
      if self.attrs['droplm'] == 0.0 and (self.train_flag or force_lm):
        self.lmmask = 1
        if recurrent_transform != 'none':
          recurrent_transform = recurrent_transform[:-3]
      elif self.attrs['droplm'] < 1.0 and (self.train_flag or force_lm):
        from theano.sandbox.rng_mrg import MRG_RandomStreams as RandomStreams
        srng = RandomStreams(self.rng.randint(1234) + 1)
        self.lmmask = T.cast(srng.binomial(n=1, p=1.0 - self.attrs['droplm'], size=self.index.shape), theano.config.floatX).dimshuffle(0,1,'x').repeat(unit.n_in,axis=2)
      else:
        self.lmmask = T.zeros_like(self.index, dtype='float32').dimshuffle(0,1,'x').repeat(unit.n_in,axis=2)

    if recurrent_transform == 'input': # attention is just a sequence dependent bias (lstmp compatible)
      src = []
      src_names = []
      n_in = 0
      for e in base:
        src_base = [ s for s in e.sources if s.name not in src_names ]
        src_names += [ s.name for s in e.sources ]
        src += [s.output for s in src_base]
        n_in += sum([s.attrs['n_out'] for s in src_base])
      self.xc = T.concatenate(src, axis=2)
      l = sqrt(6.) / sqrt(self.attrs['n_out'] + n_in)
      values = numpy.asarray(self.rng.uniform(low=-l, high=l, size=(n_in, 1)), dtype=theano.config.floatX)
      self.W_att_xc = self.add_param(self.shared(value=values, borrow=True, name = "W_att_xc"))
      values = numpy.asarray(self.rng.uniform(low=-l, high=l, size=(n_in, self.attrs['n_out'] * 4)), dtype=theano.config.floatX)
      self.W_att_in = self.add_param(self.shared(value=values, borrow=True, name = "W_att_in"))
      zz = T.exp(T.tanh(T.dot(self.xc, self.W_att_xc))) # TB1
      self.zc = T.dot(T.sum(self.xc * (zz / T.sum(zz, axis=0, keepdims=True)).repeat(self.xc.shape[2],axis=2), axis=0, keepdims=True), self.W_att_in)
      recurrent_transform = 'none'
    recurrent_transform_inst = RecurrentTransform.transform_classes[recurrent_transform](layer=self)
    assert isinstance(recurrent_transform_inst, RecurrentTransform.RecurrentTransformBase)
    unit.recurrent_transform = recurrent_transform_inst
    self.recurrent_transform = recurrent_transform_inst

    # scan over sequence
    for s in xrange(self.attrs['sampling']):
      index = self.index[s::self.attrs['sampling']]
      sequences = z
      sources = self.sources
      if encoder:
        outputs_info = [ T.concatenate([e.act[i][-1] * self.attention_weight for e in encoder], axis=1) for i in xrange(unit.n_act) ]
        sequences += T.alloc(numpy.cast[theano.config.floatX](0), n_dec, num_batches, unit.n_in) + (self.zc if recurrent_transform == 'input' else 0)
      else:
        outputs_info = [ T.alloc(numpy.cast[theano.config.floatX](0), num_batches, unit.n_out) for a in xrange(unit.n_act) ]

      if self.attrs['lm'] and self.attrs['droplm'] == 0.0 and (self.train_flag or force_lm):
        y = self.y_in[self.attrs['target']].flatten()
        sequences += self.W_lm_out[y].reshape((index.shape[0],index.shape[1],unit.n_in))

      if sequences == self.b:
        sequences += T.alloc(numpy.cast[theano.config.floatX](0), n_dec, num_batches, unit.n_in) + (self.zc if recurrent_transform == 'input' else 0)

      if unit.recurrent_transform:
        outputs_info += unit.recurrent_transform.get_sorted_state_vars_initial()

      index_f = T.cast(index, theano.config.floatX)
      unit.set_parent(self)
      outputs = unit.scan(x=sources,
                          z=sequences[s::self.attrs['sampling']],
                          non_sequences=non_sequences,
                          i=index_f,
                          outputs_info=outputs_info,
                          W_re=W_re,
                          W_in=self.W_in,
                          b=self.b,
                          go_backwards=direction == -1,
                          truncate_gradient=self.attrs['truncation'])

      if not isinstance(outputs, list):
        outputs = [outputs]
      if outputs:
        outputs[0].name = "%s.act[0]" % self.name

      if unit.recurrent_transform:
        unit.recurrent_transform_state_var_seqs = outputs[-len(unit.recurrent_transform.state_vars):]

      if self.attrs['sampling'] > 1:
        if s == 0:
          self.act = [ T.alloc(numpy.cast['float32'](0), self.index.shape[0], self.index.shape[1], n_out) for act in outputs ]
        self.act = [ T.set_subtensor(tot[s::self.attrs['sampling']], act) for tot,act in zip(self.act, outputs) ]
      else:
        self.act = outputs[:unit.n_act]
        if len(outputs) > unit.n_act:
          self.aux = outputs[unit.n_act:]
    if self.attrs['attention_store']:
      self.attention = [ self.aux[i][::direction].dimshuffle(0,2,1) for i,v in enumerate(sorted(unit.recurrent_transform.state_vars.keys())) if v.startswith('att_') ] # NBT
      for i in xrange(len(self.attention)): # TODO(zeyer): please fix LSTMC state var outputs please
        vec = T.eye(self.attention[i].shape[2], 1, direction * (self.attention[i].shape[2] - 1))
        last = vec.dimshuffle(1,'x', 0).repeat(self.index.shape[1], axis=1)
        self.attention[i] = T.concatenate([self.attention[i][1:],last],axis=0)

    if self.attrs['attention_align']:
      bp = [ self.aux[i] for i,v in enumerate(sorted(unit.recurrent_transform.state_vars.keys())) if v.startswith('K_') ]
      def backtrace(k,i_p):
        return i_p - k[i_p,T.arange(k.shape[1],dtype='int32')]
      self.alignment = []
      for K in bp: # K: NTB
        aln, _ = theano.scan(backtrace, sequences=[T.cast(K,'int32').dimshuffle(1,0,2)],
                             outputs_info=[T.cast(self.index.shape[0] - 1,'int32') + T.zeros((K.shape[2],),'int32')])
        aln = theano.printing.Print("aln")(aln)
        self.alignment.append(aln) # TB

    self.make_output(self.act[0][::direction or 1])
    self.params.update(unit.params)

  def cost(self):
    """
    :rtype: (theano.Variable | None, dict[theano.Variable,theano.Variable] | None)
    :returns: cost, known_grads
    """
    if self.unit.recurrent_transform:
      return self.unit.recurrent_transform.cost(), None
    return None, None


class LinearRecurrentLayer(HiddenLayer):
  """
  Inspired from: http://arxiv.org/abs/1510.02693
  Basically a very simple LSTM.
  """
  recurrent = True
  layer_class = "linear_recurrent"

  def __init__(self, n_out, direction=1, **kwargs):
    super(LinearRecurrentLayer, self).__init__(n_out=n_out, **kwargs)
    self.set_attr('direction', direction)
    a = T.nnet.sigmoid(self.add_param(self.create_bias(n_out, "a")))
    x = self.get_linear_forward_output()  # time,batch,n_out
    i = T.cast(self.index, dtype="float32")  # so that it can run on gpu. (time,batch)
    def step(x_t, i_t, h_p):
      i_t_bc = i_t.dimshuffle(0, 'x')  # batch,n_out
      return (a * h_p + x_t) * i_t_bc + h_p * (numpy.float32(1) - i_t_bc)
    n_batch = x.shape[1]
    h_initial = T.zeros((n_batch, n_out), dtype="float32")
    go_backwards = {1:False, -1:True}[direction]
    h, _ = theano.scan(step, sequences=[x, i], outputs_info=[h_initial], go_backwards=go_backwards)
    h = h[::direction]
    self.output = self.activation(h)

