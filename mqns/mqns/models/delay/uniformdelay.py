#    SimQN: a discrete-event simulator for the quantum networks
#    Copyright (C) 2021-2022 Lutong Chen, Jian Li, Kaiping Xue
#    University of Science and Technology of China, USTC.
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <https://www.gnu.org/licenses/>.

from typing import override

from mqns.models.delay.delay import DelayModel
from mqns.utils import rng


class UniformDelayModel(DelayModel):
    """
    Random delay from uniform distribution ``X~U(min, max)``.
    """

    def __init__(self, min=0.0, max=0.0, name="uniform") -> None:
        """
        Constructor.

        Args:
            min: minimum delay in seconds.
            max: maximum delay in seconds.
        """
        super().__init__(name)
        assert max >= min
        self._min = min
        self._max = max

    @override
    def calculate(self) -> float:
        return rng.uniform(self._min, self._max)
