from enum import Enum
from functools import total_ordering


@total_ordering
class SizeUnit(Enum):
    BYTES = 1
    KB = 2
    MB = 3
    GB = 4

    def __lt__(self, other):
        if self.__class__ is other.__class__:
            return self.value < other.value
        return NotImplemented