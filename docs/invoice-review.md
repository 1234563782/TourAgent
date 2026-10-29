# XML 发票预审第一版

## 能力与边界

登录 Aligo 工作台 → **发票预审** → 选择原始 XML → 开始预审。
聊天、JEV 和已有费用任务不变；附件不会被塞进用户原话引用。

- 支持 UTF-8（含 BOM）、根节点 `EInvoice`、`Header/Version=0.32`、`EInvoiceType/LabelCode=01` 的通用增值税票。
- 只核对 `GeneralOrSpecialVAT/LabelCode` 的 01/02；非空 `SpecificInformation`、铁路/航空等专门业务要素需独立适配。
- 不支持任意 PDF 内嵌 XML、OFD、图片、OCR，不调用语言模型抽票据。
- 解析是结构化读取，不是官方完整 XSD 认证，更不等于发票真实有效。
- 未做官方联网查验、密码学验签、作废状态查询、跨凭证一致性、报销占用、审批或付款。

xml 只是文件格式，不等于发票。只有根节点为 EInvoice 的文件才会进入发票解析；有效 XML 但根节点不是 EInvoice 的文件返回 attachment_kind=xml_other、attachment_status=unsupported，不会登记发票、查重或进入差标。若同一请求的文字已经提供完整费用事实，后续可以明确标注“仅按文字信息”继续测算；当前独立上传页面不会自动把无关 XML 转成费用任务。

## 部署

沿用项目 `.venv`、`requirements.txt`、既有 Web 登录及 memory PostgreSQL 配置，没有新增依赖。
在私密环境变量或本机 `.env` 中配置（不要提交真实票据和私密配置）：

```dotenv
TRAVEL_INVOICE_BUYER_NAME=实际单位抬头
TRAVEL_INVOICE_BUYER_TAX_ID=实际单位纳税人识别号
TRAVEL_INVOICE_MAX_FILE_BYTES=1048576
TRAVEL_INVOICE_MAX_BATCH_FILES=10
```

抬头和税号必须与学校主体一致，不从政策文件名或历史组织字段猜测。未配置则购买方待核实，不默认通过。
账号、会话和政策库均使用当前学校部署，不接受请求方传入组织字段。

管理员在仓库根目录**显式执行**：

```powershell
& './.venv/Scripts/python.exe' scripts/auth_admin.py init-invoices
```

只创建 `invoice_registry`、`invoice_files`、`invoice_reviews` 三张表，不删除旧表、不改账号身份。
正常上传与服务启动不自动执行发票 DDL。未初始化返回逐票存储失败，不伪装预审通过。
修改代码/配置后按现有方式重新启动服务；本次没有自动重启用户实例。

原始文件及完整预审结果含财务信息，存入既有 PostgreSQL 私有 schema；部署应配置备份、最小权限、磁盘加密及保留期限。
首版没有人工处理队列或用户历史浏览页；原因在当次页面呈现，完整记录持久化供后续接入。
浏览器仅在当前页面内存保留本轮文件，退出登录清空；不写 localStorage/sessionStorage，不发送第三方模型。

## 检查与结论

1. XML 大小、编码、结构、版本检查；解析前拒绝 DTD/实体声明，限制节点数和深度，不读取外部实体。
2. 必要字段、20 位数字号码、EIid 与票号一致；**没有虚构通用发票号校验位**。
3. Decimal 校验不含税金额 + 税额 = 价税合计；有明细金额时核对汇总。
4. 购买方抬头/税号与配置精确匹配，仅去两端空白，不做模糊相似匹配。
5. 蓝红字及负数识别。红票保留独立号码并转人工关联原票，不自动冲抵。
6. 单位内票号唯一；相同文件/同票不同文件不重复建实体，同票内容冲突不覆盖。

| verdict | 含义 |
| --- | --- |
| local_checks_passed | 基础检查通过；不是差标通过或验真 |
| rejected | 基础检查不通过，如购买方不符、金额勾稽不平；不宣称假票 |
| manual_review | 缺字段、红票、签名缺失等需人工核实 |
| duplicate | 本系统已接收相同票；**不代表已报销** |
| unsupported | 编码、结构、版本或特定业务要素暂不支持 |
| failed | 存储/运行失败；不是业务不合规结论 |

每项原因含 code、field、detail；已读取字段附 source=invoice、文件 SHA256、XML 路径与原值。
XML 无页码，不伪造 PDF 坐标。签名存在只得 present_not_verified，官方查验始终 not_checked。
文件哈希不证明来源可信。顶层 approved=false、reportable_amount=null。

## 差标测算

逐票展开“补充费用信息”，填写科目、实际日期、费用所在城市；身份从登录账号读取。
第一版只接受**一票完整金额关联一费用行**，契约保留多行关系，但多个分摊行转待核实，不重复累计。

- 住宿补入住/退房日期、单人入住；逐晚含税金额应与完整票额一致。多晚只有总额不均摊，待补水单。
- 开票日期从不填充入住/乘车日期，不因解析成功就设置 date_verified=true。
- 上传抬高职级/住宿标准无效；住宿与交通身份仍独立，账号未维护则待补。
- 复用 PolicyStore.estimate，保留版本、制度引用、缺失字段和逐晚结果。只测算，不调用严格报销接口放行。
- 通用票据不足以核验交通日期；可能只获得座次检查和待补材料结果。专用凭证未适配前不能宣传全额核定。
- 日期、城市、逐晚明细是用户表单补充事实，不伪装成 XML 来源；未做跨凭证核对。

某张票、某费用行失败不会清空同批其他票。基础状态与差标状态分开，不合计成“可报金额”。

## API

全部接口要求已有同源登录 Cookie；POST 要求正确 Origin，拒绝外传 user_id。
GET /api/invoices/config 返回支持范围和上传限制。

POST /api/invoices/review：

```json
{
  "files": [{
    "filename": "invoice.xml",
    "content_base64": "原始XML字节的base64",
    "expenses": []
  }]
}
```

以原始字节计算哈希，不先转码/重新排版。默认单张 1MiB、每批 10 张；只此接口扩大 body 限额，登录/聊天限制不变。
费用关联字段片段：

```json
"expenses": [{
  "line_id": "hotel-1",
  "facts": {
    "kind": "hotel", "expense_date": "2026-09-16", "check_out": "2026-09-18",
    "destination": "北京", "destination_province": "北京",
    "amount": "1000.00", "amount_scope": "stay_total", "basis": "room_night",
    "occupants": 1, "currency": "CNY", "taxes_confirmed": true,
    "nightly_amounts": [{"date":"2026-09-16","amount":"500.00"}, {"date":"2026-09-17","amount":"500.00"}]
  }
}]
```

实际费用金额必须对应该票。响应逐文件返回 results，包含理由、证据、私有预审记录 ID 和 policy_lines。
同文件重复预审不建新实体，但保留新预审记录，以记录不同费用事实/制度版本下的复查结果。

## 验证

```powershell
& './.venv/Scripts/python.exe' -X utf8 -m unittest discover -s tests -p test_invoices.py -v
node --check business_travel/web_static/invoices.js
node --test tests/chat_stream.test.cjs tests/message_view.test.cjs tests/invoices.test.cjs
```

测试票据为合成数据，真实制度 fixture 测试不代表真实票已验收。数据库并发验证需单独 opt-in 测试库。

## 参考

- [税务总局2024年第11号公告](https://fgk.chinatax.gov.cn/zcfgk/c100012/c5236067/content.html)：数电票号码与查验规则。
- [XML发票逐字段说明](https://yjtzfywh.blog.csdn.net/article/details/148611621)：adapter 字段命名公开参考，不代替官方标准。
- [公开裁剪XML样本](https://blog.csdn.net/xysoft008/article/details/145158549)：仅结构参考，未采用其法律效力/验签观点。
