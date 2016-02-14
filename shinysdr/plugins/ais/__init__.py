# Copyright 2015 Kevin Reid <kpreid@switchb.org>
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

import os.path

from twisted.web import static
from zope.interface import Interface, implements

from gnuradio import gr

try:
    from ais.radio import ais_rx
    _available = True
except ImportError:
    _available = False

from shinysdr.filters import MultistageChannelFilter
from shinysdr.modes import ModeDef, IDemodulator
from shinysdr.signals import no_signal
from shinysdr.telemetry import ITelemetryMessage, ITelemetryObject, TelemetryItem, Track, empty_track
from shinysdr.values import CollectionState, ExportedState, exported_value
from shinysdr.web import ClientResourceDef


_demod_rate = 48000  # from ais_rx
_cutoff = 12000  # slightly wider than ais_rx's filter
_transition_width = 2000


class AISDemodulator(gr.hier_block2, ExportedState):
    implements(IDemodulator)
    
    def __init__(self, mode='AIS', input_rate=0, context=None):
        assert input_rate > 0
        gr.hier_block2.__init__(
            self, self.__class__.__name__,
            gr.io_signature(1, 1, gr.sizeof_gr_complex),
            gr.io_signature(0, 0, 0))
        self.input_rate = input_rate
        
        # ais_rx has its own filter, but it is a plain freq_xlating_fir_filter and therefore potentially more expensive
        band_filter = MultistageChannelFilter(
            input_rate=input_rate,
            output_rate=_demod_rate,
            cutoff_freq=_cutoff,
            transition_width=_transition_width)
        
        self.__demod = ais_rx(
            freq=0,
            rate=_demod_rate,
            designator='A')  # TODO designator
        self.connect(
            self,
            band_filter,
            self.__demod)

    def can_set_mode(self, mode):
        return False

    def get_half_bandwidth(self):
        return _cutoff
    
    def get_output_type(self):
        return no_signal
    
    # TODO this is repeated code can we factor out "use MultistageChannelFilter and report its shape"
    @exported_value()
    def get_band_filter_shape(self):
        return {
            'low': -_cutoff,
            'high': _cutoff,
            'width': _transition_width
        }


#class AISMessage(object):
#    implements(ITelemetryMessage)


class IAISStation(Interface):
    '''marker interface for client'''
    pass


class AISStation(ExportedState):
    implements(IAISStation, ITelemetryObject)
    
    def __init__(self, object_id):
        self.__last_heard_time = None
        self.__track = empty_track
    
    # not exported
    def receive(self, message):
        '''implement ITelemetryObject'''
        self.__last_heard_time = message.receive_time
        # TODO
    
    def is_interesting(self):
        '''implement ITelemetryObject'''
        return True
    
    def get_object_expiry(self):
        '''implement ITelemetryObject'''
        return self.__last_heard_time + 60
        
    @exported_value(ctor=float)
    def get_last_heard_time(self):
        return self.__last_heard_time
    
    @exported_value(ctor=Track)
    def get_track(self):
        return self.__track


plugin_mode = ModeDef(
    mode='AIS',
    label='AIS',
    demod_class=AISDemodulator,
    available=_available)
plugin_client = ClientResourceDef(
    key=__name__,
    resource=static.File(os.path.join(os.path.split(__file__)[0], 'client')),
    load_js_path='ais.js')
