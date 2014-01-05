# Copyright 2013 Kevin Reid <kpreid@switchb.org>
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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.	See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with ShinySDR.	If not, see <http://www.gnu.org/licenses/>.

# TODO: fully clean up this GRC-generated file

from __future__ import absolute_import, division

import math
import os
import sys

from zope.interface import implements

from gnuradio import analog
from gnuradio import blocks
from gnuradio import digital
from gnuradio import filter as grfilter
from gnuradio.filter import firdes
from gnuradio import gr

from shinysdr.blocks import MultistageChannelFilter, make_resampler
from shinysdr.modes import ModeDef, IDemodulator
from shinysdr.signals import SignalType
from shinysdr.types import Range
from shinysdr.values import BlockCell, ExportedState, exported_value, setter


class RTTYDemodulator(gr.hier_block2, ExportedState):
	implements(IDemodulator)
	
	__cutoff_freq = 500

	def __init__(self, mode,
			input_rate=0,
			context=None):
		assert input_rate > 0
		gr.hier_block2.__init__(
			self, 'RTTY demodulator',
			gr.io_signature(1, 1, gr.sizeof_gr_complex * 1),
			gr.io_signature(1, 1, gr.sizeof_float * 1),
		)
		self.__text = u''
		
		baud = 45.5  # TODO param
		self.baud = baud

		demod_rate = 6000  # TODO optimize this value
		self.samp_rate = demod_rate  # TODO rename
		
		self.__channel_filter = MultistageChannelFilter(
			input_rate=input_rate,
			output_rate=demod_rate,
			cutoff_freq=self.__cutoff_freq,
			transition_width=demod_rate*0.1) # TODO optimize filter band
		self.__bit_queue = gr.msg_queue(limit=100)
		self.bit_sink = blocks.message_sink(gr.sizeof_char, self.__bit_queue, True)

		#self.connect(self, blocks.null_sink(gr.sizeof_gr_complex))
		self.connect(
			self,
			self.__channel_filter,
		#self.connect(
		#	blocks.wavfile_source("/External/Projects/sdr/GRC experiments/RTTY.wav", True),
	    #	blocks.throttle(gr.sizeof_float*1, demod_rate),
		#	grfilter.hilbert_fc(65, grfilter.firdes.WIN_HAMMING, 6.76),
			RTTYFSKDemodulator(input_rate=demod_rate, baud=baud),
			# TODO once debugging stuff is over, get rid of these extra bits
			blocks.char_to_short(1),
			blocks.add_const_vss((ord('0')*256, )),
			blocks.short_to_char(1),
			self.bit_sink)
		
		self.connect(
			self.__channel_filter,
			blocks.complex_to_real(1),
			self)
		
		self.__decoder = RTTYDecoder()
	
	def get_output_type(self):
		return SignalType(kind='MONO', sample_rate=self.samp_rate)

	def can_set_mode(self, mode):
		'''implement IDemodulator'''
		return False
	
	def get_half_bandwidth(self):
		'''implement IDemodulator'''
		return self.__cutoff_freq

	@exported_value()
	def get_band_filter_shape(self):
		return {
			'low': -self.__cutoff_freq,
			'high': self.__cutoff_freq,
			'width': self.samp_rate*0.1
		}

	@exported_value(ctor=unicode)
	def get_text(self):
		queue = self.__bit_queue
		# we would use .delete_head_nowait() but it returns a crashy wrapper instead of a sensible value like None. So implement a test (which is safe as long as we're the only reader)
		if not queue.empty_p():
			message = queue.delete_head()
			if message.length() > 0:
				bitstring = message.to_string()
			else:
				bitstring = '' # avoid crash bug
			textstring = self.__text
			for bitchar in bitstring:
				bit = bool(int(bitchar))
				r = self.__decoder(bit)
				if r is not None:
					textstring += r
			self.__text = textstring[-20:]
		return self.__text


class RTTYFSKDemodulator(gr.hier_block2):
	'''
	Demodulate FSK with parameters suitable for RTTY.
	
	TODO: Make this into something more reusable once we have other examples of FSK.
	Note this differs from the GFSK demod in gnuradio.digital by having a DC blocker.
	'''
	def __init__(self, input_rate, baud):
		gr.hier_block2.__init__(
			self, 'RTTY FSK demodulator',
			gr.io_signature(1, 1, gr.sizeof_gr_complex * 1),
			gr.io_signature(1, 1, gr.sizeof_char * 1),
		)
		
		# In order to deal with "1.5 stop bits", we use a bit rate that is twice the actual baud rate so that normal bits appear as 2 bits and the stop bit appears as 3 bits.
		self.bit_time = bit_time = input_rate / baud / 2
		
		fsk_deviation_hz = 85  # TODO param or just don't care
		
		self.__clock_recovery = digital.clock_recovery_mm_ff(
			omega=bit_time,
			gain_omega=0.25*0.05**2,
			mu=0.5,
			gain_mu=0.175,
			omega_relative_limit=0.005)
		self.__dc_blocker = grfilter.dc_blocker_ff(int(bit_time*7.5*2*20), False)
		self.__quadrature_demod = analog.quadrature_demod_cf(-input_rate/(2*math.pi*fsk_deviation_hz))
		
		self.connect(
			self,
			self.__quadrature_demod,
			self.__dc_blocker,
			self.__clock_recovery,
			digital.binary_slicer_fb(),
			self)
		

BIT_PAIR_ERROR = "<p>"
WRONG_STOP_BIT = "<w>"
MISSING_EXTRA_BIT = "<m>"

def rtty_sync_1(count):
	def rtty_sync_1_state(bit):
		if bit: return (None, rtty_sync_1(count + 1))
		elif count >= 3: return (None, rtty_same_bit(0, 1, bit))
		else: return (None, rtty_sync_1(0))
	return rtty_sync_1_state

def rtty_new_bit(accum, bits):
	def rtty_new_bit_state(bit):
		return (None, rtty_same_bit((accum << 1) + bit, bits + 1, bit))
	return rtty_new_bit_state

def rtty_same_bit(accum, bits, prev_bit):
	def rtty_same_bit_state(bit):
		if prev_bit != bit:
			return (BIT_PAIR_ERROR, rtty_sync_1(0))
		elif bits >= 7:
			if not bit:
				return (WRONG_STOP_BIT, rtty_sync_1(0))
			else:
				value = accum >> 1
				return (value, rtty_extra_stop_bit_state)
		else:
			return (None, rtty_new_bit(accum, bits))
	return rtty_same_bit_state

def rtty_extra_stop_bit_state(bit):
	if bit:
		return (None, rtty_new_bit(0, 0))
	else:
		return (MISSING_EXTRA_BIT, rtty_sync_1(0))


ITA2_LETTERS = u"nT\nO HNMrLRJIPCVEZ"  + "DBSYFQAWGfUXKl"
ITA2_FIGURES = u"n5\n- #,.r)4'80:;3\"" + "e?b6!1-2&f7/(l"


class RTTYDecoder(object):

	def __init__(self):
		self.__character_table = ITA2_LETTERS
		self.__bit_decoder = rtty_sync_1(0)

	def __call__(self, bit):
		(result, next) = self.__bit_decoder(bool(bit))
		self.__bit_decoder = next
		if isinstance(result, int):
			if result > len(self.__character_table):
				ch = '<LEN?>'
			else:
				ch = self.__character_table[result]
			if ch == 'f':
				self.__character_table = ITA2_FIGURES
				return None
			elif ch == 'l':
				self.__character_table = ITA2_LETTERS
				return None
			else:
				return ch
		return result



#dec = RTTYDecoder()
#while True:
#	c = sys.stdin.read(1)
#	if not c:
#		break
#	if c == '1':
#		bit = True
#	elif c == '0':
#		bit = False
#	else:
#		continue
#	result = dec(bit)
#	if result is not None: sys.stdout.write(result.encode())
#

# Plugin exports
pluginMode = ModeDef('RTTY', label='RTTY', demodClass=RTTYDemodulator)
