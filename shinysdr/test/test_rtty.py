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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with ShinySDR.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import absolute_import, division

from twisted.trial import unittest

from shinysdr.plugins import rtty

# TODO abstract some of this into test tools for all demodulators
class TestRTTY(unittest.TestCase):
	def __make(self):
		return rtty.RTTYDemodulator(mode='RTTY', input_rate=48000, audio_rate=32000, context=None)
	
	def test_construct(self):
		self.__make()

	def test_state(self):
		self.__make().state_to_json()