# Registration history

`registration-v1/` 保存引入论文 claim 与协议语义契约之前的 Popper 注册状态和运行日志。它绑定旧版 `paper.json`、`protocol.json`、执行器、核验器及产物哈希，仅用于迁移审计，不能作为当前任务状态继续执行。

新契约的有效运行状态只有在本机重新登记、执行后才会出现在任务根目录的 `.popper-reproduction/`；该目录被 Git 忽略，干净检出不包含它。请按上级 README 用准备脚本在新目录运行，不能把这里的旧状态当作可继续的当前任务。
