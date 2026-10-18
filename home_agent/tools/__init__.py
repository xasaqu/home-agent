"""在这里注册放在单独文件里的适配器，避免 adapters 和 xiaomi 互相导入。"""

from .adapters import ADAPTERS
from .xiaomi import MiioAdapter, XiaomiRouterAdapter

for _a in (MiioAdapter(), XiaomiRouterAdapter()):
    ADAPTERS[_a.name] = _a
