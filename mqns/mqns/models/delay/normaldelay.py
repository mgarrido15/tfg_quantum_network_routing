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


class NormalDelayModel(DelayModel):
    """
    Random delay from normal distribution ``X~N(mean_delay, std^2)``.
    """

    def __init__(self, mean=0.0, std=0.0, name="normal") -> None:
        """
        Constructor.

        Args:
            mean: mean delay in seconds.
            std: standard deviation in seconds.
        """
        super().__init__(name)
        self._mean = mean
        self._std = std

    @override
    def calculate(self) -> float:
        return rng.normal(loc=self._mean, scale=self._std)
