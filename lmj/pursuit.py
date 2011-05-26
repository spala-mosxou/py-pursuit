# Copyright (c) 2009 - 2011 Leif Johnson <leif@leifjohnson.net>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

'''Matching pursuit sparse coding algorithm and gradient ascent trainer.

Matching pursuit is a greedy sparse coding algorithm originally presented by
Mallat and Zhang (1993, IEEE Trans Sig Proc), "Matching Pursuits with
Time-Frequency Dictionaries." Using a fixed codebook (bank, etc.) of filters
(basis functions, signals, vectors, etc.), the algorithm decomposes a signal
(function, vector, etc.) into the maximally responding filter and a residual,
recursively decomposing the residual. Encoding stops after either a fixed
number of filters have been used, or until the maximal filter response drops
below some threshold. The encoding thus represents a signal as a weighted sum of
filters, with many of the weights being zero.

This module contains three implementations of matching pursuit: one for encoding
signals of a fixed shape using filters of the same shape (the Codebook class),
another for encoding signals composed of frames arranged along one dimension
(the TemporalCodebook class), and a third for encoding signals that vary in size
along two dimensions (the SpatialCodebook class). Each implementation comes
with an associated Trainer class that encapsulates the logic involved with basic
gradient-ascent training for the filters.
'''

import numpy
import logging


try:
    # define a backup correlation function in case the c module isn't present.
    import scipy.signal
    def _default_correlate(s, w, r):
        r[:] = scipy.signal.correlate(s, w, 'valid')
except ImportError:
    logging.info('cannot import scipy.signal !')
    def _default_correlate(s, w, r):
        raise NotImplementedError


def _max(a):
    return a.argmax()

def _softmax(a):
    cdf = numpy.exp(a - a.max()).cumsum()
    return numpy.searchsorted(cdf, numpy.random.uniform(0, cdf[-1]))

def _egreedy(eps=0.1):
    def choose(a):
        if numpy.random.random < eps:
            return numpy.random.randint(0, len(a) - 1)
        return a.argmax()
    return choose


class Codebook(object):
    '''Matching pursuit encodes signals using a codebook of filters.

    The encoding process decomposes a signal recursively into a maximally
    responding filter and a residual. Formally, the encoding process takes a
    signal x and produces a series of (index, coefficient) tuples (m_n, c_n)
    according to :

      x_1 = x
      w_n = argmax_w <x_n, w>
      c_n = <x_n, w_n>
      x_{n+1} = x_n - c_n * w_n

    This implementation of the algorithm is intended to encode signals of a
    constant shape, using filters of the same shape : 16x16 RGB image patches,
    10ms 2-channel audio clips, colors, etc.

    See the Trainer class for code that encapsulates a simple gradient ascent
    learning process for inferring codebook filters from data.
    '''

    def __init__(self, num_filters, filter_shape):
        '''Initialize a new codebook of static filters.

        num_filters: The number of filters to build in the codebook.
        filter_shape: A tuple of integers that specifies the shape of the
          filters in the codebook.
        '''
        if not isinstance(filter_shape, (tuple, list)):
            filter_shape = (filter_shape, )

        self.filters = numpy.random.randn(num_filters, *filter_shape)
        for w in self.filters:
            w /= numpy.linalg.norm(w)

        #self._choose = _softmax
        #self._choose = _egreedy(0.1)
        self._choose = _max

    def iterencode(self, signal, min_coeff=0., max_num_coeffs=-1):
        '''Encode a signal as a sequence of index, coefficient pairs.

        signal: A numpy array containing a signal to encode. The values in the
          array will be modified.
        min_coeff: Stop encoding when the maximal filter response drops below
          this threshold.
        max_num_coeffs: Stop encoding when this many filters have been used in
          the encoding.

        Generates a sequence of (index, coefficient) tuples.
        '''
        coeffs = numpy.array([(signal * w).sum() for w in self.filters])
        while max_num_coeffs != 0:
            max_num_coeffs -= 1

            index = self._choose(coeffs)
            coeff = coeffs[index]
            if coeff < min_coeff:
                break

            signal -= coeff * self.filters[index]

            coeffs[index] = -numpy.inf
            mask = numpy.isfinite(coeffs)
            coeffs[mask] = [(signal * w).sum() for w in self.filters[mask]]

            yield index, coeff

    def encode(self, signal, min_coeff=0., max_num_coeffs=-1):
        '''Encode a signal using our dictionary of codebook filters.

        signal: A numpy array to encode. This signal must be the same shape as
          the filters in our codebook. The values in the signal array will be
          modified.
        min_coeff: Stop encoding when the maximal filter response drops below
          this threshold.
        max_num_coeffs: Stop encoding when this many filters have been used in
          the encoding.

        Returns a tuple of (index, coefficient) tuples.
        '''
        return tuple(self.iterencode(signal, min_coeff, max_num_coeffs))

    def decode(self, encoding, unused_shape):
        '''Decode an encoding of a static signal.

        encoding: A sequence of (index, coefficient) tuples.

        Returns the sum of the filters in the encoding, weighted by their
        respective coefficients.
        '''
        try:
            return sum(c * self.filters[i] for i, c in encoding)
        except TypeError:
            return numpy.zeros_like(self.filters[0])

    def encode_frames(self, frames, min_coeff=0.1):
        '''Encode a sequence of frames.

        frames: A (possibly infinite) sequence of data frames to encode.
        min_coeff: Only fire for filters that exceed this threshold.

        Generates a sequence of ((index, coeff), ...) tuples at the same rate as
        the input frames. If a given input frame does not yield a filter
        coefficient better than the minimum threshold, the encoding output for
        that frame will be an empty tuple.
        '''
        # set up a circular buffer (2x the max length of a codebook vector).
        # http://mail.scipy.org/pipermail/scipy-user/2009-February/020108.html
        N = max(len(w) for w in self.filters)
        frame = self.filters[0][0]
        memory = numpy.zeros((2 * N, ) + frame.shape, frame.dtype)
        m = 0

        # make sure the buffer is fully pre-populated before encoding.
        frames = iter(frames)
        while m < N:
            memory[m] += frames.next()
            m += 1

        for frame in frames:
            # rotate the circular buffer from the back to the front if needed.
            if m == 2 * N:
                memory[:N] = memory[N:]
                memory[N:, :] = 0.
                m = N
            memory[m] += frame
            m += 1

            # calculate coefficients starting at offset m - N.
            window = memory[m - N:m]
            coeffs = numpy.array(
                [(window[:len(w)] * w).sum() for w in self.filters])
            encoding = []
            while True:
                index = self._choose(coeffs)
                coeff = coeffs[index]
                if coeff < min_coeff:
                    break
                encoding.append((index, coeff))
                w = self.filters[index]
                window[:len(w)] -= coeff * w
            yield tuple(encoding)

    def decode_frames(self, tuples):
        '''Given a frame encoding, decode and generate frames of output signal.

        tuples: A sequence of tuples generated by encode_frames(signal).

        Generates a sequence of signal frames at the same rate as the input
        (encoded) tuples.
        '''
        N = max(len(w) for w in self.filters)
        frame = self.filters[0][0]
        acc = numpy.zeros((2 * N, ) + frame.shape, frame.dtype)
        m = 0
        for tup in tuples:
            if m == 2 * N:
                acc[:N] = acc[N:]
                m = N
            yield acc[m]
            m += 1
            if m < N or not tup:
                continue
            index, coeff = tup
            w = self.filters[index]
            acc[m - len(w):m] += coeff * w


class Trainer(object):
    '''Train the codebook filters in a matching pursuit encoder.'''

    def __init__(self, codebook,
                 min_coeff=0., max_num_coeffs=-1,
                 momentum=0., l1=0., l2=0.):
        '''Initialize this trainer with some learning parameters.

        codebook: The matching pursuit codebook to train.
        min_coeff: Train by encoding signals to this minimum coefficient
          value.
        max_num_coeffs: Train by encoding signals using this many coefficients.
        momentum: Use this momentum value during gradient descent.
        l1: L1-regularize the codebook filters with this weight.
        l2: L2-regularize the codebook filters with this weight.
        '''
        self.codebook = codebook

        self.min_coeff = min_coeff
        self.max_num_coeffs = max_num_coeffs

        self.momentum = momentum
        self.l1 = l1
        self.l2 = l2

        self.grad = [numpy.zeros_like(w) for w in self.codebook.filters]

    def calculate_gradient(self, signal):
        '''Calculate a gradient from a signal.

        signal: A signal to use for collecting gradient information. This signal
          will be modified in the course of the gradient collection process.
        '''
        grad = numpy.zeros_like(self.grad)
        norm = [0] * len(grad)
        for index, coeff in self.codebook.iterencode(
                signal, self.min_coeff, self.max_num_coeffs):
            grad[index] += coeff * signal
            norm[index] += coeff
        return (g / (n or 1) for g, n in zip(grad, norm))

    def apply_gradient(self, grad, learning_rate):
        '''Apply gradients to the codebook filters.

        grad: A sequence of gradients to apply to the codebook filters.
        learning_rate: Move the codebook filters this much toward the gradients.
        '''
        for i, g in enumerate(grad):
            w = self.codebook.filters[i]
            l1 = numpy.clip(w, -self.l1, self.l1)
            l2 = self.l2 * w
            self.grad[i] *= self.momentum
            self.grad[i] += (1 - self.momentum) * (g - l1 - l2)
            w += learning_rate * self.grad[i]
            self._resize(i)

    def _resize(self, i):
        '''Resize codebook vector i using some energy heuristics.

        i: The index of the codebook vector to resize.

        This function is a no-op for the Trainer class.
        '''
        return

    def learn(self, signal, learning_rate):
        '''Calculate and apply a gradient from the given signal.

        signal: A signal to use for collecting gradient information. This signal
          will not be modified.
        learning_rate: Move the codebook filters this much toward the gradients.
        '''
        self.apply_gradient(
            self.calculate_gradient(signal.copy()), learning_rate)

    def reconstruct(self, signal):
        '''Reconstruct the given signal using our pursuit codebook.

        signal: A signal to encode and then reconstruct. This signal will not
          be modified.

        Returns a numpy array with the same shape as the original signal,
        containing reconstructed values instead of the original values.
        '''
        return self.codebook.decode(self.codebook.iterencode(
            signal.copy(), self.min_coeff, self.max_num_coeffs), signal.shape)


class TemporalCodebook(Codebook):
    '''Matching pursuit for convolving filters across the first dimension.

    The encoding process is recursive. Given a signal x(t) of length T that
    varies along dimension t, we calculate the inner product of each codebook
    filter w(t) with x(t - o) for all 0 < o < T - len(w), and then choose the
    filter w and offset o that result in the largest magnitude coefficient c. We
    subtract c * w(t) from x(t - o) and repeat the process with the new x(t).
    More formally,

      x_1(t) = x(t)
      w_n, o_n = argmax_{w,o} <x_n(t - o), w>
      c_n = <x_n(t - o_n), w_n>
      x_{n+1}(t) = x_n(t - o_n) - c_n * w_n

    where <a(t - o), b> denotes the inner product between a at offset o and b.
    (We use the correlation function to automate the dot product calculations at
    all offsets o.) The encoding consists of triples (w, c, o) for as many time
    steps n as desired. Reconstruction of the signal requires the codebook that
    was used at encoding time, plus a sequence of encoding triples: the
    reconstructed signal is just the weighted sum of the codebook filters at the
    appropriate offsets.

    Because we are processing signals of possibly variable length in dimension
    t, the codebook filters are allowed also to span different numbers of frames
    along dimension t. This makes the encoding more computationally complex, but
    the basic idea remains the same.

    This version of the algorithm is adopted from Smith and Lewicki (2006),
    "Efficient Auditory Coding" (Nature).
    '''

    def __init__(self, num_filters, filter_frames, frame_shape=()):
        '''Initialize a new codebook to a set of random filters.

        num_filters: The number of filters to use in our codebook.
        filter_frames: The length (in frames) of filters that we will use for
          our initial codebook.
        frame_shape: The shape of each frame of data that we will encode.
        '''
        super(TemporalCodebook, self).__init__(
            num_filters, (filter_frames, ) + frame_shape)

        self.filters = list(self.filters)

        # set up self._correlate as an alias to an appropriate correlation fn.
        try:
            import _correlate
            if len(frame_shape) == 0:
                self._correlate = _correlate.correlate1d
            if len(frame_shape) == 1:
                self._correlate = _correlate.correlate1d_from_2d
        except ImportError:
            logging.info('cannot import _correlate C module !')
            self._correlate = _default_correlate

    def iterencode(self, signal, min_coeff=0., max_num_coeffs=-1):
        '''Generate a set of codebook coefficients for encoding a signal.

        signal: A signal to encode.
        min_coeff: Stop encoding when the magnitude of coefficients falls below
          this threshold. Use 0 to encode until max_num_coeffs is reached.
        max_num_coeffs: Stop encoding when we have generated this many
          coefficients. Use a negative value to encode until min_coeff is
          reached.

        This method generates a sequence of tuples of the form (index,
        coefficient, offset), where index refers to a codebook filter and
        coefficient is the scalar multiple of the filter that is present in the
        input signal starting at the given offset.

        See the TemporalTrainer class for an example of how to use these results
        to update the codebook filters.
        '''
        def amplitude(s):
            return abs(s).sum()

        lengths = [len(w) for w in self.filters]

        # we cache the correlations between signal and codebook to avoid
        # redundant computation.
        scores = numpy.zeros(
            (len(self.filters), len(signal) - min(lengths) + 1),
            float) - numpy.inf
        for i, w in enumerate(self.filters):
            self._correlate(signal, w, scores[i, :len(signal) - len(w) + 1])

        amp = amplitude(signal)
        while max_num_coeffs != 0:
            max_num_coeffs -= 1

            # find the largest coefficient, check that it's large enough.
            flat = self._choose(scores)
            index, offset = numpy.unravel_index(flat, scores.shape)
            coeff = scores[index, offset]
            length = lengths[index]
            end = offset + length
            if coeff < min_coeff:
                break

            # check that using this filter does not increase signal amplitude.
            signal[offset:end] -= coeff * self.filters[index]
            a = amplitude(signal)
            #logging.debug('coefficient %.3g, filter %d, offset %d yields amplitude %.3g', coeff, index, offset, a)
            if a > amp:
                break
            amp = a

            # update the correlation cache for the changed part of signal.
            for i, w in enumerate(self.filters):
                l = lengths[i] - 1
                o = max(0, offset - l)
                p = min(end, len(signal) - l)
                self._correlate(signal[o:end + l], w, scores[i, o:p])

            yield index, coeff, offset

    def decode(self, coefficients, signal_shape):
        '''Decode a dictionary of codebook coefficients as a signal.

        coefficients: A sequence of (index, coefficient, offset) tuples.
        signal_shape: The shape of the reconstructed signal.

        Returns a signal that consists of the weighted sum of the codebook
        filters given in the encoding coefficients, at the appropriate offsets.
        '''
        signal = numpy.zeros(signal_shape, float)
        for index, coeff, offset in coefficients:
            w = self.filters[index]
            signal[offset:offset + len(w)] += coeff * w
        return signal


class TemporalTrainer(Trainer):
    '''Train a set of temporal codebook filters using signal data.'''

    def __init__(self, codebook,
                 min_coeff=0., max_num_coeffs=-1,
                 momentum=0., l1=0., l2=0.,
                 padding=0.1, shrink=0.005, grow=0.05):
        '''Set up the trainer with some static learning parameters.

        codebook: The matching pursuit codebook to train.
        min_coeff: Train by encoding signals to this minimum coefficient
          value.
        max_num_coeffs: Train by encoding signals using this many coefficients.
        momentum: Use this momentum value during gradient descent.
        l1: L1-regularize the codebook filters with this weight.
        l2: L2-regularize the codebook filters with this weight.
        padding: The proportion of each codebook filter to consider as "padding"
          when growing or shrinking. Values around 0.1 are usually good. None
          disables growing or shrinking of the codebook filters.
        shrink: Remove the padding from a codebook filter when the signal in the
          padding falls below this threshold.
        grow: Add padding to a codebook filter when signal in the padding
          exceeds this threshold.
        '''
        super(TemporalTrainer, self).__init__(
            codebook, min_coeff, max_num_coeffs, momentum, l1, l2)

        assert 0 <= padding < 0.5
        assert shrink < grow

        self.padding = padding
        self.shrink = shrink
        self.grow = grow

    def calculate_gradient(self, signal):
        '''Calculate a gradient from a signal.

        signal: A signal to use for collecting gradient information. This signal
          will be modified in the course of the gradient collection process.
        '''
        grad = [numpy.zeros_like(g) for g in self.grad]
        norm = [0.] * len(grad)
        for index, coeff, offset in self.codebook.iterencode(
                signal, self.min_coeff, self.max_num_coeffs):
            o = len(self.codebook.filters[index])
            grad[index] += coeff * signal[offset:offset + o]
            norm[index] += coeff
        return (g / (n or 1) for g, n in zip(grad, norm))

    def _resize(self, i):
        '''Resize codebook vector i using some energy heuristics.

        i: The index of the codebook vector to resize.
        '''
        if not 0 < self.padding < 0.5:
            return

        w = abs(self.codebook.filters[i])
        p = int(len(w) * self.padding)
        pad = numpy.zeros((p, ) + w.shape[1:], w.dtype)
        cat = numpy.concatenate

        criterion = w[:p].mean()
        #logging.debug('left criterion %.3g', criterion)
        if criterion < self.shrink:
            self.codebook.filters[i] = self.codebook.filters[i][p:]
            self.grad[i] = self.grad[i][p:]
        if criterion > self.grow:
            self.codebook.filters[i] = cat([pad, self.codebook.filters[i]])
            self.grad[i] = cat([pad, self.grad[i]])

        criterion = w[-p:].mean()
        #logging.debug('right criterion %.3g', criterion)
        if criterion < self.shrink:
            self.codebook.filters[i] = self.codebook.filters[i][:-p]
            self.grad[i] = self.grad[i][:-p]
        if criterion > self.grow:
            self.codebook.filters[i] = cat([self.codebook.filters[i], pad])
            self.grad[i] = cat([self.grad[i], pad])


class SpatialCodebook(Codebook):
    '''A matching pursuit for encoding images or other 2D signals.'''

    def __init__(self, num_filters, filter_shape, channels=0):
        '''Initialize a new codebook of static filters.

        num_filters: The number of filters to build in the codebook.
        filter_shape: A tuple of integers that specifies the shape of the
          filters in the codebook.
        channels: Set this to the number of channels in each element of the
          signal (and the filters). Leave this set to 0 if your 2D signals
          have just two values in their shape tuples.
        '''
        super(SpatialCodebook, self).__init__(
            num_filters, filter_shape + (channels and (channels, ) or ()))

        self.filters = list(self.filters)

        # set up self._correlate as an alias to an appropriate correlation fn.
        try:
            import _correlate
            if channels == 0:
                self._correlate = _correlate.correlate2d
            if channels == 3:
                self._correlate = _correlate.correlate2d_from_rgb
        except ImportError:
            logging.info('cannot import _correlate C module !')
            self._correlate = _default_correlate

    def iterencode(self, signal, min_coeff=0., max_num_coeffs=-1):
        '''Generate a set of codebook coefficients for encoding a signal.

        signal: A signal to encode.
        min_coeff: Stop encoding when the magnitude of coefficients falls below
          this threshold. Use 0 to encode until max_num_coeffs is reached.
        max_num_coeffs: Stop encoding when we have generated this many
          coefficients. Use a negative value to encode until min_coeff is
          reached.

        This method generates a sequence of tuples of the form (index,
        coefficient, (x offset, y offset)), where index refers to a codebook
        filter and coefficient is the scalar multiple of the filter that is
        present in the input signal starting at the given offsets.

        See the SpatialTrainer class for an example of how to use these
        results to update the codebook filters.
        '''
        def amplitude(s):
            return abs(s).sum()

        width, height = signal.shape[:2]
        shapes = [w.shape[:2] for w in self.filters]

        # we cache the correlations between signal and codebook to avoid
        # redundant computation.
        scores = numpy.zeros(
            (len(self.filters),
             width - min(w for w, _ in shapes) + 1,
             height - min(h for _, h in shapes) + 1),
            float) - numpy.inf
        for i, w in enumerate(self.filters):
            x, y = shapes[i]
            self._correlate(signal, w, scores[i, :width - x + 1, :height - y + 1])

        amp = amplitude(signal)
        while max_num_coeffs != 0:
            max_num_coeffs -= 1

            # find the largest coefficient, check that it's large enough.
            flat = self._choose(scores)
            index, x, y = numpy.unravel_index(flat, scores.shape)
            coeff = scores[index, x, y]
            wx, wy = shapes[index]
            ex, ey = x + wx, y + wy
            if coeff < min_coeff:
                break

            # check that using this filter does not increase signal power.
            signal[x:ex, y:ey] -= coeff * self.filters[index]
            a = amplitude(signal)
            #logging.debug('coefficient %.3g, filter %d, offset %s yields amplitude %.3g', coeff, index, (x, y), a)
            if a > amp:
                break
            amp = a

            # update the correlation cache for the changed part of signal.
            for i, w in enumerate(self.filters):
                wx, wy = shapes[i][0] - 1, shapes[i][1] - 1
                ox, oy = max(0, x - wx), max(0, y - wy)
                px, py = min(ex, width - wx), min(ey, height - wy)
                self._correlate(signal[ox:ex + wx, oy:ey + wy], w, scores[i, ox:px, oy:py])

            yield index, coeff, (x, y)

    def decode(self, coefficients, signal_shape):
        '''Decode a dictionary of codebook coefficients as a signal.

        coefficients: A sequence of (index, coefficient, offset) tuples.
        signal_shape: The shape of the reconstructed signal.

        Returns a signal that consists of the weighted sum of the codebook
        filters given in the encoding coefficients, at the appropriate offsets.
        '''
        signal = numpy.zeros(signal_shape, float)
        for index, coeff, (x, y) in coefficients:
            w = self.filters[index]
            a, b = w.shape[:2]
            signal[x:x + a, y:y + b] += coeff * w
        return signal


class SpatialTrainer(Trainer):
    '''Train a set of spatial codebook filters using signal data.'''

    def __init__(self, codebook,
                 min_coeff=0., max_num_coeffs=-1,
                 momentum=0., l1=0., l2=0.,
                 padding=0.1, shrink=0.005, grow=0.05):
        '''Set up the trainer with some static learning parameters.

        codebook: The matching pursuit codebook to train.
        min_coeff: Train by encoding signals to this minimum coefficient
          value.
        max_num_coeffs: Train by encoding signals using this many coefficients.
        momentum: Use this momentum value during gradient descent.
        l1: L1-regularize the codebook filters with this weight.
        l2: L2-regularize the codebook filters with this weight.
        padding: The proportion of each codebook filter to consider as "padding"
          when growing or shrinking. Values around 0.1 are usually good. None
          disables growing or shrinking of the codebook filters.
        shrink: Remove the padding from a codebook filter when the signal in the
          padding falls below this threshold.
        grow: Add padding to a codebook filter when signal in the padding
          exceeds this threshold.
        '''
        super(SpatialTrainer, self).__init__(
            codebook, min_coeff, max_num_coeffs, momentum, l1, l2)

        assert 0 <= padding < 0.5
        assert shrink < grow

        self.padding = padding
        self.shrink = shrink
        self.grow = grow

    def calculate_gradient(self, signal):
        '''Calculate a gradient from a signal.

        signal: A signal to use for collecting gradient information. This signal
          will be modified in the course of the gradient collection process.
        '''
        grad = [numpy.zeros_like(g) for g in self.grad]
        norm = [0.] * len(grad)
        for index, coeff, (x, y) in self.codebook.iterencode(
                signal, self.min_coeff, self.max_num_coeffs):
            w, h = self.codebook.filters[index].shape[:2]
            grad[index] += coeff * signal[x:x + w, y:y + h]
            norm[index] += coeff
        return (g / (n or 1) for g, n in zip(grad, norm))

    def _resize(self, i):
        '''Resize codebook vector i using some energy heuristics.

        i: The index of the codebook vector to resize.
        '''
        if not 0 < self.padding < 0.5:
            return

        w = abs(self.codebook.filters[i])
        p = int(w.shape[0] * self.padding)
        q = int(w.shape[1] * self.padding)

        cat = numpy.concatenate
        pad = numpy.zeros((p, ) + w.shape[1:], w.dtype)

        criterion = w[:p].mean()
        #logging.debug('top criterion %.3g', criterion)
        if criterion < self.shrink:
            self.codebook.filters[i] = self.codebook.filters[i][p:]
            self.grad[i] = self.grad[i][p:]
        if criterion > self.grow:
            self.codebook.filters[i] = cat([pad, self.codebook.filters[i]])
            self.grad[i] = cat([pad, self.grad[i]])

        criterion = w[-p:].mean()
        #logging.debug('bottom criterion %.3g', criterion)
        if criterion < self.shrink:
            self.codebook.filters[i] = self.codebook.filters[i][:-p]
            self.grad[i] = self.grad[i][:-p]
        if criterion > self.grow:
            self.codebook.filters[i] = cat([self.codebook.filters[i], pad])
            self.grad[i] = cat([self.grad[i], pad])

        cat = numpy.hstack
        pad = numpy.zeros((len(self.codebook.filters[i]), p) + w.shape[2:], w.dtype)

        criterion = w[:, :q].mean()
        #logging.debug('left criterion %.3g', criterion)
        if criterion < self.shrink:
            self.codebook.filters[i] = self.codebook.filters[i][:, q:]
            self.grad[i] = self.grad[i][:, q:]
        if criterion > self.grow:
            self.codebook.filters[i] = cat([pad, self.codebook.filters[i]])
            self.grad[i] = cat([pad, self.grad[i]])

        criterion = w[:, -q:].mean()
        #logging.debug('right criterion %.3g', criterion)
        if criterion < self.shrink:
            self.codebook.filters[i] = self.codebook.filters[i][:, :-q]
            self.grad[i] = self.grad[i][:, :-q]
        if criterion > self.grow:
            self.codebook.filters[i] = cat([self.codebook.filters[i], pad])
            self.grad[i] = cat([self.grad[i], pad])

