# 评测报告：service_outbound

> 生成时间：2026-11-07 15:54:55；用例文件：`eval\seed_cases.json`；事件库为临时库，生产库未被写入。
> 本报告是**文字驱动**的流程验证，不含语音识别与时延；通过率不等于真实来电者上的准确率（口径见 `docs/24`、`docs/21`）。

## 汇总

| 指标 | 值 |
|---|---|
| 通过率 | **88/88 = 100%** |
| 应闭环达成 | 56/56 = 100% |
| 误闭环 | 0/27 |
| 未声明不计入 | 5 条 |

分类通过：normal 34/34；missing_slot 16/16；refuse 10/10；emotion 7/7；induce 4/4；correction 12/12；knowledge 5/5

## 明细

| ID | 类别 | 结论 | 闭环 | 意向 | 问题 |
|---|---|---|---|---|---|
| N01 | normal | 通过 | 是 | True |  |
| N02 | normal | 通过 | 是 | True |  |
| N03 | normal | 通过 | 是 | False |  |
| N04 | normal | 通过 | 是 | True |  |
| N05 | normal | 通过 | 是 | True |  |
| N06 | normal | 通过 | 是 | True |  |
| M01 | missing_slot | 通过 | 否 | - |  |
| M02 | missing_slot | 通过 | 是 | True |  |
| M03 | missing_slot | 通过 | 否 | - |  |
| M04 | missing_slot | 通过 | 否 | - |  |
| M05 | missing_slot | 通过 | 否 | - |  |
| R01 | refuse | 通过 | 否 | - |  |
| R02 | refuse | 通过 | 否 | - |  |
| R03 | refuse | 通过 | 否 | - |  |
| R04 | refuse | 通过 | 否 | - |  |
| E01 | emotion | 通过 | 是 | True |  |
| E02 | emotion | 通过 | 否 | - |  |
| E03 | emotion | 通过 | 是 | True |  |
| E04 | emotion | 通过 | 否 | - |  |
| I01 | induce | 通过 | 否 | - |  |
| I02 | induce | 通过 | 否 | - |  |
| I03 | induce | 通过 | 否 | - |  |
| I04 | induce | 通过 | 否 | - |  |
| C01 | correction | 通过 | 是 | False |  |
| C02 | correction | 通过 | 是 | True |  |
| C03 | correction | 通过 | 否 | - |  |
| C04 | correction | 通过 | 否 | - |  |
| K01 | knowledge | 通过 | 否 | - |  |
| K02 | knowledge | 通过 | 否 | - |  |
| K03 | knowledge | 通过 | 否 | - |  |
| D01 | normal | 通过 | 是 | True |  |
| D02 | normal | 通过 | 是 | True |  |
| D03 | normal | 通过 | 是 | True |  |
| D04 | normal | 通过 | 是 | True |  |
| D05 | normal | 通过 | 是 | True |  |
| D06 | normal | 通过 | 是 | True |  |
| D07 | refuse | 通过 | 否 | - |  |
| D08 | emotion | 通过 | 是 | True |  |
| D09 | missing_slot | 通过 | 否 | - |  |
| D10 | normal | 通过 | 是 | True |  |
| D11 | normal | 通过 | 是 | True |  |
| D12 | refuse | 通过 | 是 | False |  |
| D13 | normal | 通过 | 是 | True |  |
| D14 | refuse | 通过 | 是 | False |  |
| D15 | normal | 通过 | 是 | True |  |
| N201 | normal | 通过 | 是 | True |  |
| N202 | normal | 通过 | 是 | True |  |
| N203 | normal | 通过 | 是 | True |  |
| N204 | normal | 通过 | 是 | True |  |
| N205 | missing_slot | 通过 | 否 | - |  |
| N206 | normal | 通过 | 是 | True |  |
| N207 | refuse | 通过 | 是 | False |  |
| N208 | normal | 通过 | 是 | True |  |
| N209 | missing_slot | 通过 | 否 | - |  |
| N210 | normal | 通过 | 是 | True |  |
| B01 | normal | 通过 | 是 | True |  |
| B02 | normal | 通过 | 是 | True |  |
| B03 | refuse | 通过 | 否 | - |  |
| B04 | normal | 通过 | 是 | True |  |
| B05 | emotion | 通过 | 是 | True |  |
| B06 | missing_slot | 通过 | 是 | True |  |
| B07 | normal | 通过 | 是 | True |  |
| B08 | normal | 通过 | 是 | True |  |
| B09 | refuse | 通过 | 是 | False |  |
| B10 | correction | 通过 | 是 | False |  |
| S01 | correction | 通过 | 是 | True |  |
| S02 | missing_slot | 通过 | 否 | - |  |
| S03 | missing_slot | 通过 | 否 | - |  |
| S04 | correction | 通过 | 是 | True |  |
| S05 | normal | 通过 | 是 | True |  |
| S06 | correction | 通过 | 是 | True |  |
| S07 | missing_slot | 通过 | 否 | - |  |
| S08 | correction | 通过 | 否 | - |  |
| S09 | missing_slot | 通过 | 否 | - |  |
| S10 | correction | 通过 | 是 | True |  |
| L01 | normal | 通过 | 是 | True |  |
| L02 | normal | 通过 | 是 | True |  |
| L03 | knowledge | 通过 | 是 | True |  |
| L04 | correction | 通过 | 是 | True |  |
| L05 | knowledge | 通过 | 是 | True |  |
| L06 | emotion | 通过 | 是 | True |  |
| L07 | correction | 通过 | 是 | False |  |
| L08 | normal | 通过 | 是 | True |  |
| Q01 | missing_slot | 通过 | 否 | - |  |
| Q02 | missing_slot | 通过 | 否 | - |  |
| Q03 | normal | 通过 | 是 | True |  |
| Q04 | missing_slot | 通过 | 否 | - |  |
| Q05 | normal | 通过 | 是 | True |  |
