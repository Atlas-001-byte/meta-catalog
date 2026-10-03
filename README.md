# Meta Catalog

元数据目录与检索服务：Schema 注册、影响分析、全文检索，以及字段级破坏性变更
识别与变更追溯检索。纯 Python 标准库实现，不依赖外部同类组件。

## 能力概览

1. **Schema 注册**：按 `名称@版本` 注册不可变 JSON Schema；支持跨 Schema
   `$ref`（形如 `Address@1.0#/properties/street`），允许前向引用与引用环，
   目标 Schema 已注册但引用字段不存在时注册即报错。
2. **目录资产**：注册直接引用某些 Schema 字段的资产（服务、任务、报表等）。
3. **影响分析**：给定字段，返回直接引用它的资产，以及经其他 Schema
   `$ref` 传递引用它的资产；传递影响按稳定路径去重。
4. **字段级变更识别**：比较同一 Schema 的基线版本与候选文档（可附显式字段
   重命名映射），生成可直接展示、可检索的字段级变更报告，报告关联原 Schema
   标识与版本。
5. **升级影响汇总**：`analyze_upgrade_impact` 按资产汇总一次升级的命中情况
   （直接 / 传递 / 两者），候选解析、重命名、字段路径与 `report_id` 语义与
   字段级比较一致，但不生成或改写报告与索引。
6. **全文检索**：Schema、资产与报告中的每条字段变更统一入索引，支持关键词
   与结构化条件组合检索。

## 快速开始

```python
from meta_catalog import MetaCatalog
from meta_catalog.errors import SchemaComparisonInvalid, ImpactAnalysisTooLarge

catalog = MetaCatalog()

catalog.register_schema("Person", "1.0", {
    "type": "object",
    "properties": {
        "name":  {"type": "string"},
        "age":   {"type": "integer"},
        "level": {"type": "string", "enum": ["a", "b"]},
    },
    "required": ["name"],
})

catalog.register_asset("svc-order", "订单服务", "service", refs=[
    {"schema": "Person", "version": "1.0", "path": "/name"},
])

report = catalog.compare_schemas(
    "Person", "1.0",
    {
        "type": "object",
        "properties": {
            "name":  {"type": "string"},
            "years": {"type": "integer"},          # age 改名为 years
            "level": {"type": "string", "enum": ["a", "b", "c"]},  # 放宽枚举
            "nick":  {"type": "string"},           # 新增非必填
        },
        "required": ["name"],
    },
    candidate_version="2.0",
    renames=[{"from": "/age", "to": "/years"}],
)

# 读取 / 追溯检索
catalog.get_report(report["report_id"])
catalog.search(schema="Person", version="2.0", compatibility="breaking")
catalog.search(change_kind="rename", asset_name="订单")
```

## 字段路径

字段路径采用逻辑化 JSON Pointer：

- 对象属性省略 `properties` 段：`/properties/name` 写作 `/name`；
- 数组元素写作 `-`：`/properties/tags/items` 写作 `/tags/-`；
- `additionalProperties` 写作 `*`；
- 根对象为空串 `""`。

资产注册时既接受文档指针也接受逻辑路径，注册时统一结合文档结构解析。

## 变更种类与兼容结论

每条结果包含：字段路径、`old_path`/`new_path`、旧定义摘要、新定义摘要、
变更种类、兼容结论、直接影响资产、传递影响资产。

| 变更种类 `change_kind` | 含义 | 兼容结论 |
|---|---|---|
| `added` | 新增属性 | 非必填 `compatible`；必填 `breaking` |
| `deleted` | 删除属性 | `breaking` |
| `modified` | 结构定义变化 | 按下表判定 |
| `required_added` | 既有字段新增必填约束 | `breaking` |
| `required_removed` | 既有字段解除必填约束 | `compatible` |
| `rename` | 显式声明的字段重命名（整棵子树按一次计入） | `breaking` |
| `metadata` | 仅 `title`/`description`/`$comment` 变化 | `metadata` |

`modified` 的兼容规则：

- **兼容**：放宽枚举、`integer` 扩展为 `number`、增加默认值、取消/放宽数值与
  长度边界、扩大类型集合；
- **破坏**：收窄类型或枚举、新增枚举约束、新增边界约束、允许原本不接受的
  `null`、移除或改变默认值；
- 同时存在放宽与收窄时，按 **breaking** 处理。

显式重命名映射按一次 `rename` 计入结果，不再同时计为删除和新增；映射子树中
未被覆盖的删除与新增仍分别呈现。结构变化与新增必填同时发生时，种类记为
`modified`，兼容结论取更严格的 `breaking`。

## 重命名映射规则

`renames=[{"from": 旧路径, "to": 新路径}, ...]`，以下情形统一返回错误码
`SchemaComparisonInvalid`：

- 候选文档不是合法 JSON Schema；
- 指定的基线/候选版本不存在；
- 重命名起点在基线版本不存在，或终点在候选版本不存在；
- 同一路径（含端点）被重复映射，或映射端点的子树相互重叠；
- 起点仍存在于候选版本、或终点已存在于基线版本（跨版本不一致）。

## 影响分析

- **直接影响**：资产引用与目标字段 `(Schema, 版本, 路径)` 精确相等。
- **传递影响**：沿跨 Schema `$ref` 边反向传播，按字段后缀对齐；引用整个
  对象或更深子路径的资产同样命中。结果按
  `(资产标识, Schema, 版本, 稳定路径)` 去重并稳定排序。
- 影响链深度、遍历边数、影响资产数量、单次比较的分析对象字段数与变更条目数
  超过公开限制（见 `meta_catalog/limits.py`）时统一返回
  `ImpactAnalysisTooLarge`。

## 升级影响汇总

`catalog.analyze_upgrade_impact(name, baseline_version, candidate=None, *,
candidate_version=None, renames=None, asset_ids=None)`：

- 候选文档、候选版本、重命名映射与字段路径语义与 `compare_schemas` 完全一致，
  返回的 `report_id` 与同输入的 `compare_schemas` 相同；
- 该调用是只读分析：**不**生成或改写注册内容、报告与检索索引，之后同输入
  `compare_schemas` 仍正常入库且结果一致；
- `asset_ids=None`（默认）覆盖全部资产；指定列表时去重、与顺序无关，只汇总
  所列资产；未知资产返回 `NotFound`；
- 候选文档非法、版本不存在或重命名不合法返回 `SchemaComparisonInvalid`，
  字段数/变更数/影响链等超过公开限制返回 `ImpactAnalysisTooLarge`；
- 返回可 JSON 序列化 dict，顶层依次为 `report_id`、`schema`、`baseline_version`、
  `candidate_version`、`summary`、`assets`；
- `summary` 含 `asset_total`、`breaking_assets`、`compatible_assets`、
  `metadata_assets`、`unaffected_assets`、`changed_paths`（入选资产命中项的
  `path` 去重数）；
- `assets` 按 `asset_id` 升序并包含未受影响资产（`status="unaffected"`、
  `changes=[]`）；每项含 `asset_id`、`name`、`kind`、`status`、`changes`；
- 资产状态取其命中变更兼容结论的最严重者，严重顺序为
  `breaking > compatible > metadata > unaffected`；
- 每条命中保留变更报告的全部字段并增加 `impact_kind`：
  `direct`（直接命中）、`transitive`（传递命中）、`both`（同时直接与传递
  命中）；同一资产对同一条报告变更只保留一条，与同输入其他分析结论一致。

## 检索

`catalog.search(keyword, **filters)`，条件之间为 AND：

- `keyword`：普通关键词，按固定分词口径（拉丁连续串、中文二元组、小写化、
  AND 全命中）匹配可检索文本字段；
- 结构化过滤：`schema`、`version`、`field_path`、`change_kind`、
  `compatibility`、`asset_name`（命中直接或传递影响资产名称）、`doc_type`、
  `limit`；
- 返回条目标注 `matched_fields`（命中字段）与命中资产摘要；
- 排序为「命中字段数降序 + 确定性键升序」，与索引插入顺序无关。报告进入
  索引不改变旧关键词查询的匹配口径与排序稳定性。

## 不可变性与边界

- Schema 版本、版本关系与资产依赖关系注册后不可变；重复注册返回
  `AlreadyExists`；所有读取返回深拷贝。
- 内联候选文档不会被注册；比较、报告读取与检索都不会改写既有注册内容。
- 报告只通过公开接口返回，不规定落盘格式；相同输入产生相同 `report_id`、
  相同字段顺序、影响顺序与兼容结论。
- 错误码：`NotFound`、`AlreadyExists`、`SchemaComparisonInvalid`、
  `ImpactAnalysisTooLarge`。

## 测试

```bash
python3 -m unittest discover -s tests
```
