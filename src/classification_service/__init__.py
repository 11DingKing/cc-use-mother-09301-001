"""高校分类定位论证服务端。

模块划分：

- ``canonical``：规范 JSON 与内容哈希，保证跨进程可复算。
- ``db``：SQLite 结构与连接初始化。
- ``store``：数据访问与写事务（BEGIN IMMEDIATE）。
- ``rules``：版本化分类规则包与确定性评分引擎。
- ``workflow``：申请、证据、回避、签署、复议等用例与状态机。
- ``httpapi``：线程化 HTTP/JSON 接口与幂等键处理。
"""

__version__ = "0.2.0"
