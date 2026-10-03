# Meta Catalog

元数据目录与检索服务：Schema 注册、影响分析、全文检索，以及字段级
破坏性变更识别与变更追溯检索。纯 Python 标准库实现，无外部依赖。

## 能力概览

1. **Schema 注册**：按 `名称@版本` 注册 JSON Schema（常用关键字子集，
   含本地 `$ref`、跨 Schema `catalog://` 引用、`allOf` 内联展开）。
   注册内容不可变。
2. **影响分析**：给定字段路径，返回直接引用该字段（子树）的资产，
   以及沿 Schema 间 `$ref` 反向追溯的传递引用资产；按稳定路径去重，
   每个资产保留字典序最小的最短链。
3. **全文检索**：资产按关键词 AND 检索，大小写不敏感、标识符拆词，
   排序稳定。
4. **字段级变更比较（新增）**：提交基线版本、候选版本（已注册版本或
   内联候选文档）与显式字段重命名映射，产出按字段路径的确定性变更
   报告，关联原 Schema 标识与版本，并自动纳入检索范围。
5. **变更追溯检索（新增）**：按 Schema 名称、版本、字段路径、变更
   种类、兼容结论、影响资产名称组合检索（条件 AND），返回命中字段
   与命中资产摘要。

## 快速开始

```python
from meta_catalog import Catalog

catalog = Catalog()
catalog.register_schema("User", "1.0.0", {
    "type": "object",
    "required": ["id"],
    "properties": {
        "id":   {"type": "integer"},
        "name": {"type": "string"},
        "age":  {"type": "integer"},
    },
})
catalog.register_schema("User", "2.0.0", {
    "type": "object",
    "required": ["id", "email"],
    "properties": {
        "id":       {"type": "integer"},
        "fullName": {"type": "string"},
        "age":      {"type": "number"},
        "email":    {"type": "string"},
    },
})
catalog.register_asset(
    "etl-user", "User ETL Job", "job",
    references=[{"schema": "User", "version": "1.0.0", "field": "/name"}],
)

report = catalog.compare_schemas(
    "User", "1.0.0", "2.0.0",
    renames=[{"from": "/name", "to": "/fullName"}],
)
# -> report_id / schema / baseline_version / candidate_version / renames / changes

catalog.get_report(report["report_id"])

catalog.search_changes(compatibility="breaking", impacted_asset="ETL")
catalog.search_assets("user")
```

## 字段路径

JSON Pointer 风格：根为 `/`，属性以 `/` 分隔（`/address/city`），
数组元素形状用 `[]`（`/tags/[]`），元组下标用 `/0`。资产引用省略
字段时表示引用整个 Schema 根。

## 变更种类与兼容结论

每条结果包含：`field_path`、`kind`、`compatibility`、`old`（旧定义
摘要）、`new`（新定义摘要）、`impacted_assets`（直接与传递影响，
含深度与稳定影响链）；重命名结果另含 `renamed_to`。

| kind | 含义 | compatibility |
| --- | --- | --- |
| `added` | 新增非必填属性 | compatible |
| `added_required` | 新增必填属性 | breaking |
| `removed` | 删除属性 | breaking |
| `renamed` | 显式声明的重命名（一次 rename，不再同时计删除与新增） | breaking |
| `required_added` | 既有属性新增必填约束 | breaking |
| `required_relaxed` | 取消必填 | compatible |
| `nullable_added` | 开始接受原本不接受的 null | breaking |
| `type_widened` | 类型放宽（含 `integer` → `number`） | compatible |
| `type_narrowed` / `type_changed` | 类型收窄 / 互不包含的类型变化 | breaking |
| `enum_relaxed` / `enum_narrowed` | 枚举放宽 / 收窄（互不包含按破坏计） | compatible / breaking |
| `default_added` | 增加默认值 | compatible |
| `constraint_relaxed` | 数值/长度上界放宽、约束移除、format 变化等 | compatible |
| `constraint_narrowed` / `constraint_changed` | 数值/长度/正则收窄 / 同时收窄与放宽 | breaking |
| `const_changed` | const 变化 | breaking |
| `ref_changed` | 跨 Schema 引用变化 | breaking |
| `metadata_changed` | 仅 `title` / `description` / `$comment` 变化 | metadata |

判定优先级：当同一字段同时存在多种变化时，按
必填新增 → nullable → 类型 → 枚举 → const → 边界/正则 → 引用 →
兼容类 → 元数据 的固定顺序给出唯一结论。

### 重命名映射

- 支持 `{"from": "/a", "to": "/b"}`、`("/a", "/b")` 两种写法，
  路径可省略前导 `/`。
- 未被映射覆盖的删除与新增仍分别以 `removed` / `added` 呈现；
  旧路径被删除而同一新路径被另一 rename 终点占用时，旧身份仍单独
  计为 `removed`。
- 跨版本一致性：系统从成功报告中汇总字段身份链（如
  `/b`→`/c`→`/d`）。新提交的映射若导致同一字段身份在某一版本上
  对应两条不同路径，返回 `SchemaComparisonInvalid`。

## 检索

- `search_assets(query)`：基线关键词检索，口径与排序保持不变
  （拆词后全部命中；命中词数降序、asset_id 升序）。
- `search_changes(...)`：`schema` / `version`（精确匹配基线或候选
  版本之一）/ `field_path`（按路径拆词 AND，含 `renamed_to`）/
  `kind` / `compatibility` / `impacted_asset`（按资产名/ID 拆词
  AND）/ `query`（自由关键词，同资产口径）任意组合，条件之间 AND。
  返回 `total`、`total_fields` 与 `hits`；每个 hit 含
  `matched_fields` 与 `matched_assets` 摘要，顺序稳定。

## 错误码

| 错误码 | 触发场景 |
| --- | --- |
| `SchemaComparisonInvalid` | 候选文档不是合法 JSON Schema；基线/候选版本不存在；重命名起点或终点不存在；同一路径被重复映射；rename 自映射；重命名映射跨版本不一致 |
| `ImpactAnalysisTooLarge` | 比较/分析对象字段数超过限制；影响链深度或受影响资产数超过限制 |
| `SchemaInvalid` | 注册的 Schema 文档不合法 |
| `SchemaNotFound` / `VersionNotFound` / `AlreadyExists` | 读取或注册冲突 |

错误以异常抛出：`SchemaComparisonInvalid`、`ImpactAnalysisTooLarge`
（均为 `CatalogError` 子类），`.error_code` 为稳定错误码字符串。

## 公开限制（`meta_catalog.limits.LIMITS`）

| 键 | 默认值 | 含义 |
| --- | --- | --- |
| `max_fields_per_schema` | 10 000 | 单 Schema 扁平化字段路径数 |
| `max_fields_per_comparison` | 20 000 | 单次比较两侧去重字段路径总数 |
| `max_impact_depth` | 50 | Schema 间传递引用最大跳数 |
| `max_impacted_assets` | 5 000 | 单条变更字段影响资产数上限 |
| `max_schema_bytes` | 4 MiB | 单个 Schema 文档大小 |

## 不变性与确定性

- 比较、报告读取与检索都不会改写已有 Schema 注册内容、版本关系或
  资产依赖；内联候选文档不落盘。
- 相同输入（rename 映射与提交顺序无关）产生字段顺序、影响顺序、
  兼容结论与 `report_id` 完全一致的报告；重复提交相同比较幂等
  （同一 `report_id` 保留一份）。
- 成功结果只通过公开接口返回，不规定落盘格式。

## 支持的 JSON Schema 子集

`type`（含类型数组）、`properties`、`patternProperties`（校验）、
`required`、`items`、`prefixItems`、`enum`、`const`、`default`、
`format`、`pattern`、数值/长度边界、`allOf`（约束按交集合并）、
`$defs` / `definitions`、布尔 Schema、本地 `$ref` 与
`catalog://<名称>[@版本][#<Pointer>]` 跨 Schema 引用。
`anyOf` / `oneOf` / `not` 参与校验但不展开为确定字段路径。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 约定

- 公开行为以 README 与源码为准。
- 后续需求在此基线上增量实现。
