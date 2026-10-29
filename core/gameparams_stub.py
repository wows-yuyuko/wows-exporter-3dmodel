"""
GameParams 桩类 —— pickle.loads 反序列化旧版 .data 文件所需。

Wargaming 的 GameParams.data 使用 pickle 序列化，其中引用了
GameParams.TypeInfo / GPData / GameParams / UIParams 等类。
此文件提供最小桩类，使 pickle.loads 能成功加载数据。

★ pickle 记录的是**顶层模块名** `GameParams`（不是包路径），所以反序列化前
必须把它挂进 sys.modules：

    from core import gameparams_stub
    sys.modules["GameParams"] = gameparams_stub

（`core/gameparams.py::_ensure_stub` 与 `services/processor_service.py` 各一处。）
类名同样不能改。
"""


class TypeInfo(object):
    pass


class GPData(object):
    pass


class GameParams:
    pass


class UIParams:
    pass
