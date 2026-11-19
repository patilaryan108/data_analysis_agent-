# Power BI plan for `upload_data_clean`

Power BI table name used in the DAX: **BlinkIT Grocery Data** (change it with the PBI_TABLE setting or the `pbi_table` argument).

## 1. Measures
Add one at a time with **Modeling > New measure**.

**Total Height** - Headline total of Height (format: `#,0`)
```DAX
Total Height = SUM('BlinkIT Grocery Data'[Height])
```

**Average Height** - Typical Height per row (format: `#,0.00`)
```DAX
Average Height = AVERAGE('BlinkIT Grocery Data'[Height])
```

**Row Count** - Number of rows in the current filter (format: `#,0`)
```DAX
Row Count = COUNTROWS('BlinkIT Grocery Data')
```

**Average Weight** - Typical Weight (format: `0.00`)
```DAX
Average Weight = AVERAGE('BlinkIT Grocery Data'[Weight])
```

**Average Legendary** - Typical Legendary (format: `0.00`)
```DAX
Average Legendary = AVERAGE('BlinkIT Grocery Data'[Legendary])
```

**Height Share %** - Each bar or slice as a share of the total (slicers still apply) (format: `0.0%`)
```DAX
Height Share % = DIVIDE([Total Height], CALCULATE([Total Height], ALLSELECTED('BlinkIT Grocery Data')))
```

## 2. Pages: Overview (+ Details if there are more than 6 charts)

- **Top row, cards:** [Total Height], [Average Height], [Average Weight]
- **Left rail, slicers:** none suggested

| Where | Visual | Fields | Answers |
|---|---|---|---|
| Overview, row 1, column 1 | Clustered bar chart | Y-axis: Type2; X-axis: [Total Height]; sort high to low | Which Type2 brings in the most Height |
| Overview, row 1, column 2 | Clustered bar chart | Y-axis: Type1; X-axis: [Total Height]; sort high to low | Which Type1 brings in the most Height |
| Overview, row 2, column 1 | Clustered column chart | X-axis: Type2; Legend: Type1; Y-axis: [Total Height] | Compare Type1 inside each Type2 |
| Overview, row 2, column 2 | Clustered bar chart | Y-axis: Type2; X-axis: [Average Weight] | Does average Weight differ by Type2? |
| Overview, row 3, column 1 | Clustered bar chart | Y-axis: Type1; X-axis: [Average Weight] | Does average Weight differ by Type1? |
| Overview, row 3, column 2 | Clustered bar chart | Y-axis: Type2; X-axis: [Average Legendary] | Does average Legendary differ by Type2? |
| Details page, row 1, column 1 | Clustered bar chart | Y-axis: Type1; X-axis: [Average Legendary] | Does average Legendary differ by Type1? |
| Details page, row 1, column 2 | Scatter chart | X-axis: Weight; Y-axis: Height | Relationship between Weight and Height |

## 3. Page 2: AI Insights (optional)
Get data > MySQL > `agent_insights` (it appears after the agent saves its first insight). Add a **Table** visual with Created At, Question, Answer, and press Refresh after new insights.
