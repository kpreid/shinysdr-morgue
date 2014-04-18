# Copyright 2013, 2014 Kevin Reid <kpreid@switchb.org>
#
# This file is part of ShinySDR.
# 
# ShinySDR is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# ShinySDR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with ShinySDR.  If not, see <http://www.gnu.org/licenses/>.

# TODO: fully clean up this GRC-generated file

from __future__ import absolute_import, division

from twisted.web import static
from zope.interface import implements

from gnuradio import blocks
from gnuradio import analog
from gnuradio import fft
from gnuradio import filter as grfilter  # don't shadow builtin
from gnuradio.filter import firdes
from gnuradio.filter import optfir

import math
import os.path

from shinysdr.blocks import make_resampler
from shinysdr.modes import ModeDef, IDemodulator
from shinysdr.plugins.basic_demod import SimpleAudioDemodulator, make_lofi_audio_filter
from shinysdr.values import exported_value, setter
from shinysdr.web import ClientResourceDef

audio_modulation_index = 0.07
fm_subcarrier = 9960
fm_deviation = 480


class VOR(SimpleAudioDemodulator):
	implements(IDemodulator)
	
	def __init__(self, mode='VOR', zero_point=59, **kwargs):
		self.channel_rate = channel_rate = 40000
		internal_audio_rate = 20000  # TODO over spec'd
		self.zero_point = zero_point

		transition = 5000
		SimpleAudioDemodulator.__init__(self,
			mode=mode,
			demod_rate=channel_rate,
			band_filter=fm_subcarrier * 1.25 + fm_deviation + transition / 2,
			band_filter_transition=transition,
			**kwargs)

		self.dir_rate = dir_rate = 10

		if internal_audio_rate % dir_rate != 0:
			raise ValueError('Audio rate %s is not a multiple of direction-finding rate %s' % (internal_audio_rate, dir_rate))
		self.dir_scale = dir_scale = internal_audio_rate // dir_rate
		self.audio_scale = audio_scale = channel_rate // internal_audio_rate

		self.dir_vector_filter = grfilter.fir_filter_ccf(1, firdes.low_pass(
			1, dir_rate, 1, 2, firdes.WIN_HAMMING, 6.76))
		self.am_channel_filter_block = grfilter.fir_filter_ccf(1, firdes.low_pass(
			1, channel_rate, 5000, 5000, firdes.WIN_HAMMING, 6.76))
		self.goertzel_fm = fft.goertzel_fc(channel_rate, dir_scale * audio_scale, 30)
		self.goertzel_am = fft.goertzel_fc(internal_audio_rate, dir_scale, 30)
		self.fm_channel_filter_block = grfilter.freq_xlating_fir_filter_ccc(1, (firdes.low_pass(1.0, channel_rate, fm_subcarrier / 2, fm_subcarrier / 2, firdes.WIN_HAMMING)), fm_subcarrier, channel_rate)
		self.multiply_conjugate_block = blocks.multiply_conjugate_cc(1)
		self.complex_to_arg_block = blocks.complex_to_arg(1)
		am_agc_samples = 1024
		self.am_agc_block = analog.feedforward_agc_cc(am_agc_samples, 1.0)
		self.am_demod_block = analog.am_demod_cf(
			channel_rate=channel_rate,
			audio_decim=audio_scale,
			audio_pass=5000,
			audio_stop=5500,
		)
		self.fm_demod_block = analog.quadrature_demod_cf(1)
		self.phase_agc_fm = analog.agc2_cc(1e-1, 1e-2, 1.0, 1.0)
		self.phase_agc_am = analog.agc2_cc(1e-1, 1e-2, 1.0, 1.0)
		
		self.probe = blocks.probe_signal_f()
		
		self.audio_filter_block = make_lofi_audio_filter(internal_audio_rate)
		self.resampler_block = make_resampler(internal_audio_rate, self.audio_rate)

		# Compute filter-introduced delays on AM vs. FM path:
		def sampleDelay(filter):
			print 'filter taps = ', len(filter.taps())
			return float(len(filter.taps()))
		# AM-only delay:
		am_delay = 0.0
		# am_channel_filter_block
		am_delay += sampleDelay(self.am_channel_filter_block) / channel_rate
		print 'A', am_delay
		# am_agc_block
		am_delay += float(am_agc_samples) / channel_rate
		print 'B', am_delay
		# am_demod_block <- changes rate
		am_delay += float(len(optfir.low_pass(0.5, 	   # Filter gain
		                             channel_rate, # Sample rate
					     5000,   # Audio passband
					     5500,   # Audio stopband
					     0.1, 	   # Passband ripple
					     60))) / internal_audio_rate
		print 'C', am_delay
		# ...goertzel_am
		# FM-only delay:
		fm_delay = 0.0
		# fm_channel_filter_block
		fm_delay += sampleDelay(self.fm_channel_filter_block) / channel_rate
		print 'D', am_delay
		# fm_demod_block
		# zero delay
		# ...goertzel_fm
		print am_delay, fm_delay
		
		relative_delay = (am_delay - fm_delay) / 30 * (2*math.pi)
		print 'Phase delay', relative_delay

		self.zeroer = blocks.add_const_vff((zero_point * (math.pi / 180) - relative_delay, ))
		
		##################################################
		# Connections
		##################################################
		# Input
		self.connect(
			self,
			self.band_filter_block)
		# AM chain
		self.connect(
			self.band_filter_block,
			self.am_channel_filter_block,
			self.am_agc_block,
			self.am_demod_block)
		# AM audio
		self.connect(
			self.am_demod_block,
			blocks.multiply_const_ff(1.0 / audio_modulation_index * 0.5),
			self.audio_filter_block,
			self.resampler_block)
		self.connect_audio_output(self.resampler_block, self.resampler_block)
		
		# AM phase
		self.connect(
			self.am_demod_block,
			self.goertzel_am,
			self.phase_agc_am,
			(self.multiply_conjugate_block, 0))
		# FM phase
		self.connect(
			self.band_filter_block,
			self.fm_channel_filter_block,
			self.fm_demod_block,
			self.goertzel_fm,
			self.phase_agc_fm,
			(self.multiply_conjugate_block, 1))
		# Phase comparison and output
		self.connect(
			self.multiply_conjugate_block,
			self.dir_vector_filter,
			self.complex_to_arg_block,
			blocks.multiply_const_ff(-1),  # opposite angle conventions
			self.zeroer,
			self.probe)

	@exported_value(ctor=float)
	def get_zero_point(self):
		return self.zero_point

	@setter
	def set_zero_point(self, zero_point):
		self.zero_point = zero_point
		self.zeroer.set_k((self.zero_point * (math.pi / 180), ))

	@exported_value(ctor=float)
	def get_angle(self):
		return self.probe.level()


# Twisted plugin exports
pluginMode = ModeDef('VOR', label='VOR', demodClass=VOR)
pluginClient = ClientResourceDef(
	key=__name__,
	resource=static.File(os.path.join(os.path.split(__file__)[0], 'client')),
	loadURL='vor.js')
