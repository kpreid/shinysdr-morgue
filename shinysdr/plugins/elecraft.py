# Copyright 2016 Kevin Reid <kpreid@switchb.org>
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

"""
Plugin for controlling Elecraft radios (K3, K3s, KX3, K2).
"""

from __future__ import absolute_import, division

import struct
import sys

from twisted.internet import defer
from twisted.internet.protocol import Factory, Protocol
from twisted.protocols.basic import LineReceiver
from twisted.python import log
from zope.interface import implements

from shinysdr.devices import Device, IComponent
from shinysdr.interfaces import IHasFrequency
from shinysdr.types import Enum, Notice, Range
from shinysdr.twisted_ext import FactoryWithArgs, SerialPortEndpoint
from shinysdr.values import ExportedState, LooseCell, ViewCell, exported_value


__all__ = []  # appended later


# TODO this is named as it is to match the Hamlib plugin; reevaluate what is a good consistent naming scheme.
@defer.inlineCallbacks
def connect_to_rig(reactor, port, baudrate=38400):
    """
    Start a rigctld process and connect to it.
    
    port: Serial port device name.
    baudrate: Serial data rate; must match that set on the radio.
    """
    endpoint = SerialPortEndpoint(port, reactor, baudrate=baudrate)
    factory = FactoryWithArgs.forProtocol(_ElecraftClientProtocol, reactor)
    protocol = yield endpoint.connect(factory)
    proxy = protocol._proxy()
    
    defer.returnValue(Device(
        vfo_cell=proxy.iq_center_cell(),
        components={'rig': proxy}))


class _ElecraftRadio(ExportedState):
    # TODO: Tell protocol to do no/less polling when nobody is looking.
    implements(IComponent, IHasFrequency)
    
    def __init__(self, protocol):
        self.__cache_freq = 0
        self.__protocol = protocol
        self.__init_center_cell()
    
    def __init_center_cell(self):
        st = self.state()
        base_freq_cell = st['freq']
        mode_cell = st['mode']
        sidetone_cell = st['sidetone']
        iq_offset_cell = LooseCell(key='iq_offset', value=0.0, type=float)
        
        self.__iq_center_cell = ViewCell(
                base=base_freq_cell,
                get_transform=lambda x: x + iq_offset_cell.get(),
                set_transform=lambda x: x - iq_offset_cell.get(),
                key='freq',
                type=base_freq_cell.type(),  # runtime variable...
                writable=True,
                persists=base_freq_cell.persists())
        
        def changed_iq():
            # TODO this is KX3-specific
            mode = mode_cell.get()
            if mode == 'CW':
                iq_offset = sidetone_cell.get()
            elif mode == 'CW-REV':
                iq_offset = -sidetone_cell.get()
            elif mode == 'AM' or mode == 'FM':
                iq_offset = 11000.0
            else:  # USB, LSB, other
                iq_offset = 0.0
            iq_offset_cell.set(iq_offset)
            self.__iq_center_cell.changed_transform()
        
        mode_cell.subscribe(changed_iq)
        sidetone_cell.subscribe(changed_iq)
        changed_iq()
    
    def state_def(self, callback):
        """overrides ExportedState"""
        ic = lambda *a, **kwa: _install_cell(self, callback, *a, **kwa)

        # main knobs
        ic('freq', Range([(100000, 99999999999)], integer=True))
        ic('b_freq', Range([(100000, 99999999999)], integer=True))
        ic('af_gain', Range([(0, 255)], integer=True), sub=True)
        ic('rf_gain', Range([(0, 250)], integer=True), sub=True)
        ic('mode', Enum({x: x for x in _mode_strings if x is not None}, strict=False), sub=True) # TODO enum
        ic('data_mode', unicode, sub=True) # TODO enum

        # VFO-related options
        ic('channel', int)
        ic('vfo_lock', bool, sub=True)
        ic('vfo_link', bool)
        ic('split', bool)
        ic('diversity', bool)
        ic('scan', bool)
        ic('sub_on', bool)
        ic('band', int, sub=True)
        ic('vfo_rx', Enum({'0': 'A', '1': 'B'}))
        ic('vfo_tx', Enum({'0': 'A', '1': 'B'}))
        ic('rit_on', bool)
        ic('xit_on', bool)
        ic('rit_offset', Range([(-9999, 9999)], integer=True))

        # rx signal parameters
        ic('bandwidth', Range([(0, 9999)], integer=True), sub=True)  # TODO encode the 10 Hz step
        ic('if_shift', Range([(0, 9999)], integer=True))
        ic('agc_time', Range([(2, 2), (4, 4)], integer=True))
        ic('noise_blanker', bool, sub=True)
        ic('rx_atten', unicode, sub=True)
        ic('rx_preamp', bool, sub=True)
        ic('squelch', Range([(0, 29)], integer=True), sub=True)
        ic('XFIL', unicode, sub=True)
        ic('if_freq', Range([(0, 9999)], integer=True))  # TODO adjust range after doing value translation
        
        # tx signal parameters
        ic('compression', Range([(0, 40)], integer=True))
        ic('essb', bool)
        ic('keyer_speed', Range([(8, 50)], integer=True))
        ic('mic_gain', Range([(0, 60)], integer=True))
        ic('monitor_level', Range([(0, 60)], integer=True))
        ic('sidetone', Range([(300, 800)], integer=True))
        ic('tx_power_set', Range([(0, 110)], integer=True))
        #ic('tx_power_act', Range([(0, 110)], integer=True))  # not being fetched
        ic('vox', bool)

        # misc parameters
        ic('antenna', Enum({'1': '1', '2': '2'}))
    
    def close(self):
        """implements IComponent"""
        self.__protocol.transport.loseConnection()
    
    def iq_center_cell(self):
        return self.__iq_center_cell
    
    @exported_value(type=Notice(always_visible=False))
    def get_errors(self):
        error = self.__protocol.get_communication_error()
        if not error:
            return u''
        elif error == u'not_responding':
            return u'Radio not responding.'
        elif error == u'bad_data':
            return u'Bad data from radio.'
        else:
            return unicode(error)
    
    def _send_command(self, command):
        # hook used by _install_cell to send commands
        self.__protocol.send_command(command)


def _install_cell(self, callback, key, type, sub=False):
    # pylint: disable=redefined-builtin
    def actually_write_value(value):
        if key == 'freq':
            self._send_command('FA%011d;' % (value,))
    
    # TODO give the types a 'give me a dummy value' protocol
    if isinstance(type, Range):
        initial_value = type(0)  # TODO does not work for strict ranges
    elif isinstance(type, Enum):
        initial_value = type.get_table().keys()[0]
    elif type == unicode:
        initial_value = u''
    elif type == bool:
        initial_value = False
    elif type == int:
        initial_value = 0
    else:
        initial_value = None
    
    writable = key == 'freq'  # TODO others
    cell = LooseCell(
        key=key,
        value=initial_value,
        type=type,
        writable=writable,
        persists=False,
        post_hook=actually_write_value)
    callback(cell)
    
    if sub:
        _install_cell(self, callback,
            key=key + '$',
            type=type,
            sub=False)


def _digit_bool(text):
    return bool(int(text))


def _scaled_int(scale):
    return lambda text: float(text) * scale


_mode_strings = [None, 'LSB', 'USB', 'CW', 'FM', 'AM', 'DATA', 'CW-REV', None, 'DATA-REV']


def _decode_mode(text):
    try:
        s = _mode_strings[int(text)]
        if s is None:
            return text
        return s
    except ValueError:
        return text
    except IndexError:
        return text


class _ElecraftClientProtocol(Protocol):
    def __init__(self, reactor):
        self.__reactor = reactor
        self.__line_receiver = LineReceiver()
        self.__line_receiver.delimiter = ';'
        self.__line_receiver.lineReceived = self.__lineReceived
        self.__proxy_obj = _ElecraftRadio(self)
        self.__communication_error = u'not_responding'
        
        # set up dummy initial state
        self.__scheduled_poll = reactor.callLater(0, lambda: None)
        self.__scheduled_poll.cancel()
    
    def connectionMade(self):
        """overrides Protocol"""
        self.__reinitialize()
    
    def connectionLost(self, reason):
        # pylint: disable=signature-differs
        """overrides Protocol"""
        if self.__scheduled_poll.active():
            self.__scheduled_poll.cancel()
        self.__communication_error = u'serial_gone'
    
    def get_communication_error(self):
        return self.__communication_error
    
    def dataReceived(self, data):
        """overrides Protocol"""
        self.__line_receiver.dataReceived(data)
    
    _commands = {
        # ! not used
        # @ not used
        'AG': ('af_gain', int),
        # AI not used as GET
        # AK not used
        'AN': ('antenna', int),
        'AP': ('apf', _digit_bool),
        # BG not yet used
        'BN': ('band', int),
        'BW': ('bandwidth', _scaled_int(10)),
        'CP': ('compression', int),
        'CW': ('sidetone', _scaled_int(10)),
        # DB not used
        # DS not used
        'DT': ('data_mode', int),
        'DV': ('diversity', _digit_bool),
        'ES': ('essb', _digit_bool),
        'FA': ('freq', int),
        'FB': ('b_freq', int),
        'FI': ('if_freq', int),  # TODO value translation
        'FR': ('vfo_rx', unicode),
        'FT': ('vfo_tx', unicode),
        'FW': ('bandwidth', _scaled_int(10)),
        'GT': ('agc_time', int),
        # IC not used
        # ID not yet used
        # IF handled separately
        'IS': ('if_shift', int),
        'K2': None,
        'K3': None,  # TODO use it to convert
        'KS': ('keyer_speed', int),
        # KY not yet used
        'LK': ('vfo_lock', _digit_bool),
        'LN': ('vfo_link', _digit_bool),
        'MC': ('channel', int),
        'MD': ('mode', _decode_mode),
        'MG': ('mic_gain', int),
        'ML': ('monitor_level', int),
        # MN, MP, MQ not used
        'NB': ('noise_blanker', _digit_bool),
        # NL requires special handling
        # OM requires special handling
        'PA': ('rx_preamp', _digit_bool),  # note does not handle value=2
        'PC': ('tx_power_set', int),  # note NOT using "K2 extended" format
        'PO': ('tx_power_act', _scaled_int(0.1)),  # not yet handling "QRO mode"
        # PS not used
        'RA': ('rx_atten', unicode),  # TODO value translation
        'RG': ('rf_gain', int),  # TODO value translation
        'RO': ('rit_offset', int),
        'RT': ('rit_on', _digit_bool),
        # RV not used
        'SB': ('sub_on', _digit_bool),
        # SD not yet used
        # SM not yet used
        'SQ': ('squelch', int),
        # TB not yet used
        # TQ not yet used
        'VX': ('vox', int),
        'XF': ('XFIL', int),
        'XT': ('xit_on', _digit_bool),
    }
    
    def send_command(self, cmd_text):
        """Send a raw command.
        
        The text must include its trailing semicolon.
        """
        assert cmd_text == '' or cmd_text.endswith(';')
        self.transport.write(cmd_text)
    
    def __reinitialize(self):
        # TODO: Use ID, K3, and OM commands to confirm identity and customize
        self.transport.write(
            'AI2;'  # Auto-Info Mode 2; notification of any change (delayed)
            'K31;')  # enable extended response, important for FW command
        self.request_all()  # also triggers polling cycle
    
    def __schedule_timeout(self):
        if self.__scheduled_poll.active():
            self.__scheduled_poll.cancel()
        self.__scheduled_poll = self.__reactor.callLater(1, self.__poll_doubtful)
    
    def __schedule_got_response(self):
        if self.__scheduled_poll.active():
            self.__scheduled_poll.cancel()
        self.__scheduled_poll = self.__reactor.callLater(0.04, self.__poll_fast_reactive)
    
    def request_all(self):
        self.transport.write(
            'IF;'
            'AG;AG$;AN;AP;BN;BN$;BW;BW$;CP;CW;DV;ES;FA;FB;FI;FR;FT;GT;IS;KS;'
            'LK;LK$;LN;MC;MD;MD$;MG;ML;NB;NB$;PA;PA$;PC;RA;RA$;RG;RG$;SB;SQ;'
            'SQ$;VX;XF;XF$;')
        # If we don't get a response, this fires
        self.__schedule_timeout()
    
    def __poll_doubtful(self):
        """If this method is called then we didn't get a prompt response."""
        self.__communication_error = 'not_responding'
        self.transport.write('FA;')
        self.__schedule_timeout()
    
    def __poll_fast_reactive(self):
        """Normal polling activity."""
        # Get FA (VFO A frequency) so we respond fast.
        # Get BN (band) because if we find out we changed bands we need to update band-dependent things.
        self.transport.write('FA;BN;')
        self.__schedule_timeout()
    
    def __lineReceived(self, line):
        line = line.lstrip('\x00')  # nulls are sometimes sent on power-on
        #log.msg('Elecraft client: received %r' % (line,))
        if '\x00' in line:
            # Bad data that may be received during radio power-on
            return
        elif line == '?':
            # busy indication; nothing to do about it as yet
            pass
        else:
            try:
                cmd = line[:2]
                sub = len(line) > 2 and line[2] == '$'
                data = line[(3 if sub else 2):]
                if cmd == 'IF':
                    IF_STRUCT = struct.Struct('!11s 5x 5s s s 3x s s s s s s s 2x')
                    (freq, rit_offset, rit_on, xit_on, _tx, mode, vfo_rx, scan, split, _band_changed, data_mode) = IF_STRUCT.unpack_from(data)
                    self._update('freq', int(freq))
                    self._update('rit_offset', int(rit_offset))
                    self._update('rit_on', _digit_bool(rit_on))
                    self._update('xit_on', _digit_bool(xit_on))
                    self._update('mode', _decode_mode(mode))
                    self._update('vfo_rx', int(vfo_rx))
                    self._update('scan', _digit_bool(scan))
                    self._update('split', _digit_bool(split))
                    self._update('data_mode', int(data_mode))
                elif cmd in self._commands:
                    cmd_handling = self._commands[cmd]
                    if cmd_handling is not None:
                        (key, coercer) = cmd_handling
                        self._update(key + ('$' if sub else ''), coercer(data))
                else:
                    log.msg('Elecraft client: unrecognized message %r' % (line,))
            except ValueError as e:  # bad digits or whatever
                log.err(e, 'Elecraft client: error while parsing message %r' % (line,))
                self.__communication_error = 'bad_data'
                return  # don't consider as OK, but don't reinit either
        
        if not self.__communication_error:
            # communication is still OK
            self.__schedule_got_response()
        else:
            self.__communication_error = None
            # If there was a communication error, we might be out of sync, so resync and also start up normal polling.
            self.__reinitialize()
    
    def _update(self, key, value):
        """Handle a received value update."""
        # TODO less poking at internals, more explicit
        cell = self.__proxy_obj.state().get(key)
        if cell:
            old_value = cell.get()
            cell.set_internal(value)
        else:
            # just don't crash
            log.msg('Elecraft client: missing cell for state %r' % (key,))
        if key == 'band' and value != old_value:
            # Band change! Check the things we don't get prompt or any notifications for.
            self.request_all()
    
    def _proxy(self):
        """for use by connect_to_rig"""
        return self.__proxy_obj


def frequency_editor_experiment_main():
    from twisted.internet.task import deferLater, react
    
    @defer.inlineCallbacks
    def body(reactor):
        print >>sys.stderr, 'started up'
        port, = sys.argv[1:]
        device = yield connect_to_rig(reactor, port)
        proxy = device.get_components_dict()['rig']
        print >>sys.stderr, 'operating'
        yield deferLater(reactor, 0.1, lambda: None)  # TODO fudge factor
        proxy._send_command('MC001;')
        yield deferLater(reactor, 0.1, lambda: None)  # TODO fudge factor
        print device.get_freq()
        print >>sys.stderr, 'exiting'
    
    react(body)

if __name__ == '__main__':
    frequency_editor_experiment_main()
