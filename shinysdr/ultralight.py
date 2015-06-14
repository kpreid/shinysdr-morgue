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


'''
TX-supporting main object for low-end computers.
'''


from __future__ import absolute_import, division

#import math
#import time
#
#from twisted.internet import reactor
#from twisted.python import log
from zope.interface import Interface, implements  # available via Twisted

from gnuradio import analog
from gnuradio import audio
from gnuradio import blocks
from gnuradio import gr
from gnuradio.filter import rational_resampler

from shinysdr.audiomux import AudioManager
#from shinysdr.blocks import MonitorSink, RecursiveLockBlockMixin, Context
from shinysdr.filters import make_resampler
from shinysdr.math import dB, todB
from shinysdr.modes import get_modes, lookup_mode
#from shinysdr.receiver import Receiver
#from shinysdr.signals import SignalType
from shinysdr.types import Enum, Range, Notice
from shinysdr.values import ExportedState, exported_value, setter, BlockCell


__all__ = []  # appended later


#AUDIO_RATE = 16000

TX_AUDIO_RATE = 22050


class TwoWayRadio(ExportedState):
    def __init__(self, device, audio_config):
        
        self.__mode = mode = 'USB'
        self.__ptt = False
        
        self.device = device
        self.rx = _RxTop(device=device, mode=mode, audio_config=audio_config)
        self.tx = _TxTop(device=device, mode=mode)
        
        self.rx.start()
        #self.tx.start()

    def state_def(self, callback):
        super(TwoWayRadio, self).state_def(callback)
        # TODO make this possible to be decorator style
        callback(BlockCell(self, 'device'))
        callback(BlockCell(self, 'rx'))
        callback(BlockCell(self, 'tx'))

    def close_all_devices(self):
        '''Close all devices in preparation for a clean shutdown.'''
        self.device.close()
        ending = self.tx if self.__ptt else self.rx
        ending.stop()
        ending.wait()

    # type construction is deferred because we don't want loading this file to trigger loading plugins
    # TODO: Filter to exclude stereo and nonaudio modes (not yet possible generically)
    @exported_value(ctor_fn=lambda self: Enum({d.mode: d.label for d in get_modes() if d.mod_class is not None}))
    def get_mode(self):
        return self.__mode
    
    @setter
    def set_mode(self, mode):
        mode = unicode(mode)
        if mode == self.__mode: return
        self.rx.set_mode(mode)
        self.tx.set_mode(mode)
        self.__mode = mode

    @exported_value(ctor=bool, persists=False)
    def get_ptt(self):
        return self.__ptt
    
    @setter
    def set_ptt(self, value):
        value = bool(value)
        if value == self.__ptt: return
        
        if value:
            ending, starting = self.rx, self.tx
        else:
            ending, starting = self.tx, self.rx
        
        self.__ptt = value
        
        ending.lock()
        # At this point, (ending) is locked and (starting) is stopped.
        def midpoint():
            ending.unlock()
            ending.stop()
        
        self.device.set_transmitting(value, midpoint)
        ending.wait()
        starting.start()

    # TODO move these methods to a facet of AudioManager
    def add_audio_queue(self, queue, queue_rate):
        self.rx._audio_manager.add_audio_queue(queue, queue_rate)
        self.rx._do_connect()
    
    def remove_audio_queue(self, queue):
        self.rx._audio_manager.remove_audio_queue(queue)
        self.rx._do_connect()
    
    def get_audio_queue_channels(self):
        '''
        Return the number of channels (which will be 1 or 2) in audio queue outputs.
        '''
        return self.rx._audio_manager.get_channels()

__all__.append('TwoWayRadio')


class _RxTop(gr.top_block, ExportedState):
    def __init__(self, device, mode, audio_config):
        gr.top_block.__init__(self, type(self).__name__)
        
        self.__audio_gain = -6.0
        
        self.__device = device
        self.demodulator = None
        self.__audio_gain_block = blocks.multiply_const_ff(0.0)
        #self.__audio_sink = audio.sink(AUDIO_RATE, '', False)
        
        # TODO: We will want an RF meter of some sort, lacking a waterfall to warn us alternately
        #self.__clip_probe = MaxProbe()
        
        self._audio_manager = AudioManager(
            graph=self,
            audio_config=audio_config,
            stereo=False)
        
        self.set_audio_gain(self.get_audio_gain())  # init value
        self.__rebuild_demodulator(mode)
        self._do_connect()
    
    def state_def(self, callback):
        super(_RxTop, self).state_def(callback)
        # TODO make this possible to be decorator style
        callback(BlockCell(self, 'demodulator'))

    def _do_connect(self):
        self.lock()
        # TODO: fail on stereo (?)
        self.disconnect_all()
        self.connect(
            self.__device.get_rx_driver(),
            self.demodulator,
            self.__audio_gain_block)
        
        audio_rs = self._audio_manager.reconnecting()
        audio_rs.input(self.__audio_gain_block, self.demodulator.get_output_type().get_sample_rate(), 'server')
        audio_rs.finish_bus_connections()  # TODO implement stop on no client...?
        
        self.unlock()
    
    @exported_value(ctor=Range([(-30, 20)], strict=False))
    def get_audio_gain(self):
        return self.__audio_gain

    @setter
    def set_audio_gain(self, value):
        self.__audio_gain = float(value)
        self.__audio_gain_block.set_k(dB(value))
    
    def set_mode(self, mode):
        if self.demodulator and self.demodulator.can_set_mode(mode):
            self.demodulator.set_mode(mode)
        else:
            self.__rebuild_demodulator(mode)
            self._do_connect()
    
    def __rebuild_demodulator(self, mode):
        self.demodulator = lookup_mode(mode).demod_class(
            mode=mode,
            input_rate=self.__device.get_rx_driver().get_output_type().get_sample_rate(),
            context=None)


class _TxTop(gr.top_block, ExportedState):
    def __init__(self, device, mode):
        gr.top_block.__init__(self, type(self).__name__)
        
        assert device.can_transmit()
        
        self.__device = device
        self.__mode = mode
        
        self.__audio_resampler = None
        self.modulator = None
        self.__upsampler = None

        self.__rebuild_modulator(mode)
        
        # TODO update on modulator change
        #self.__audio_source = analog.sig_source_f(self.modulator.get_input_type().get_sample_rate(), analog.GR_SAW_WAVE, -1, 2000, 1000)
        self.__audio_source = audio.source(
            sampling_rate=TX_AUDIO_RATE,
            device_name='',  # TODO configurable / use client
            ok_to_block=False)
        self.__audio_gain_block = blocks.multiply_const_ff(6.0)
        self.__audio_probe = analog.probe_avg_mag_sqrd_f(0, alpha=9.6 / TX_AUDIO_RATE)
        
        self._do_connect()
    
    def state_def(self, callback):
        super(_TxTop, self).state_def(callback)
        # TODO make this possible to be decorator style
        callback(BlockCell(self, 'modulator'))

    def _do_connect(self):
        self.lock()
        try:
            self.disconnect_all()
            self.connect(
                self.__audio_source,
                self.__audio_gain_block,
                self.__audio_resampler,
                self.modulator,
                self.__upsampler,
                self.__device.get_tx_driver())
            self.connect(self.__audio_resampler, self.__audio_probe)
        finally:
            self.unlock()

    def set_mode(self, mode):
        self.__rebuild_modulator(mode)
        self._do_connect()

    def __rebuild_modulator(self, mode):
        rf_sample_rate = self.__device.get_tx_driver().get_input_type().get_sample_rate()
        modulator = self.modulator = lookup_mode(mode).mod_class(
            mode=mode,
            context=None)
        self.__audio_resampler = rational_resampler.rational_resampler_fff(
            interpolation=int(modulator.get_input_type().get_sample_rate()),
            decimation=TX_AUDIO_RATE)
        self.__upsampler = rational_resampler.rational_resampler_ccf(  # TODO use shinysdr.filters tools
            interpolation=int(rf_sample_rate),
            decimation=int(modulator.get_output_type().get_sample_rate()))
    
    @exported_value(ctor=Range([(-30, 0)], strict=False))
    def get_audio_power(self):
        return todB(max(1e-10, self.__audio_probe.level()))
