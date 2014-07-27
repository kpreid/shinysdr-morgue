# Copyright 2014 Kevin Reid <kpreid@switchb.org>
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

from __future__ import absolute_import, division

from zope.interface import implements

from gnuradio import gr
from gnuradio import blocks
from gnuradio import analog

from shinysdr.modes import ModeDef, IDemodulator
from shinysdr.values import BlockCell, ExportedState, exported_value
from shinysdr.blocks import MonitorSink, MultistageChannelFilter


_sample_rate = 80000


class ScopeDemod(gr.hier_block2, ExportedState):
    # Not literally a demodulator but that's the interface
    implements(IDemodulator)
    
    def __init__(self, mode='Scope', input_rate=0, audio_rate=0, context=None):
        assert input_rate > 0
        gr.hier_block2.__init__(
            self, 'Scope',
            gr.io_signature(1, 1, gr.sizeof_gr_complex * 1),
            # TODO: Add generic support for demodulators with no audio output
            gr.io_signature(2, 2, gr.sizeof_float * 1),
        )
        
        # TODO live adjustable
        self.__cutoff_freq = 30000
        self.__transition_width = 20000
        
        self.__filter = MultistageChannelFilter(
            input_rate=input_rate,
            output_rate=_sample_rate,
            cutoff_freq=self.__cutoff_freq,
            transition_width=self.__transition_width)
        
        self.monitor = MonitorSink(
            sample_rate=_sample_rate,
            complex_in=True,
            enable_scope=True,
            context=context)
        
        # Scope chain
        self.connect(self, self.__filter, self.monitor)
        
        # Dummy audio
        zero = analog.sig_source_f(0, analog.GR_CONST_WAVE, 0, 0, 0)
        throttle = blocks.throttle(gr.sizeof_float, audio_rate)
        self.connect(zero, throttle)
        self.connect(throttle, (self, 0))
        self.connect(throttle, (self, 1))

    def state_def(self, callback):
        super(ScopeDemod, self).state_def(callback)
        # TODO make this possible to be decorator style
        callback(BlockCell(self, 'monitor'))
    
    def can_set_mode(self, mode):
        return False

    def get_half_bandwidth(self):
        return self.__cutoff_freq

    def set_rec_freq(self, freq):
        '''for ITunableDemodulator'''
        self.__filter.set_center_freq(freq)

    @exported_value()
    def get_band_filter_shape(self):
        return {
            'low': -self.__cutoff_freq,
            'high': self.__cutoff_freq,
            'width': self.__transition_width
        }


pluginDef = ModeDef('Scope', label='(Scope)', demodClass=ScopeDemod)
