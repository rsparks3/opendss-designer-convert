import math

# Line-to-line / line-to-neutral voltage ratio. Upstream used 1.732, which put
# every converted kV base about 0.003 % off (4.1599 kV instead of 4.16 kV).
LL_LN_CONVERSION_FACTOR = math.sqrt(3)
