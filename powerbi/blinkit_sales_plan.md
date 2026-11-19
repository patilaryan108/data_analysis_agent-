# Power BI plan for `blinkit_sales`

Power BI table name used in the DAX: **BlinkIT Grocery Data** (change it with the PBI_TABLE setting or the `pbi_table` argument).

## 1. Measures
Add one at a time with **Modeling > New measure**.

**Total Sales** - Headline total of Sales (format: `#,0`)
```DAX
Total Sales = SUM('BlinkIT Grocery Data'[Sales])
```

**Average Sales** - Typical Sales per row (format: `#,0.00`)
```DAX
Average Sales = AVERAGE('BlinkIT Grocery Data'[Sales])
```

**Row Count** - Number of rows in the current filter (format: `#,0`)
```DAX
Row Count = COUNTROWS('BlinkIT Grocery Data')
```

**Item Count** - How many different items (format: `#,0`)
```DAX
Item Count = DISTINCTCOUNT('BlinkIT Grocery Data'[Item Identifier])
```

**Outlet Count** - How many different outlets (format: `#,0`)
```DAX
Outlet Count = DISTINCTCOUNT('BlinkIT Grocery Data'[Outlet Identifier])
```

**Average Sales per Outlet** - Sales per outlet; stops big groups winning just because they have more outlets (format: `#,0`)
```DAX
Average Sales per Outlet = DIVIDE([Total Sales], [Outlet Count])
```

**Average Rating** - Typical Rating (format: `0.00`)
```DAX
Average Rating = AVERAGE('BlinkIT Grocery Data'[Rating])
```

**Average Item Visibility** - Typical Item Visibility (format: `0.00`)
```DAX
Average Item Visibility = AVERAGE('BlinkIT Grocery Data'[Item Visibility])
```

**Average Item Weight** - Typical Item Weight (format: `0.00`)
```DAX
Average Item Weight = AVERAGE('BlinkIT Grocery Data'[Item Weight])
```

**Sales Share %** - Each bar or slice as a share of the total (slicers still apply) (format: `0.0%`)
```DAX
Sales Share % = DIVIDE([Total Sales], CALCULATE([Total Sales], ALLSELECTED('BlinkIT Grocery Data')))
```

## 2. Pages: Overview (+ Details if there are more than 6 charts)

- **Top row, cards:** [Total Sales], [Average Sales], [Item Count], [Outlet Count], [Average Rating]
- **Left rail, slicers:** Item Category, Item Fat Content, Outlet Location Type, Outlet Size, Outlet Type

| Where | Visual | Fields | Answers |
|---|---|---|---|
| Overview, row 1, column 1 | Clustered bar chart | Y-axis: Outlet Type; X-axis: [Average Sales per Outlet]; sort high to low | Typical Sales per outlet for each Outlet Type (fair comparison) |
| Overview, row 1, column 2 | Clustered bar chart | Y-axis: Item Type; X-axis: [Total Sales]; sort high to low | Which Item Type brings in the most Sales |
| Overview, row 2, column 1 | Clustered column chart | X-axis: Outlet Establishment Year; Y-axis: [Total Sales] | Sales across Outlet Establishment Year (columns, not an area chart: these are separate values) |
| Overview, row 2, column 2 | Donut chart | Legend: Outlet Location Type; Values: [Total Sales]; show percentage | Share of Sales by Outlet Location Type |
| Overview, row 3, column 1 | Donut chart | Legend: Outlet Type; Values: [Total Sales]; show percentage | Share of Sales by Outlet Type |
| Overview, row 3, column 2 | Donut chart | Legend: Item Fat Content; Values: [Total Sales]; show percentage | Share of Sales by Item Fat Content |
| Details page, row 1, column 1 | Clustered column chart | X-axis: Item Fat Content; Legend: Outlet Location Type; Y-axis: [Total Sales] | Compare Outlet Location Type inside each Item Fat Content |
| Details page, row 1, column 2 | Clustered bar chart | Y-axis: Item Fat Content; X-axis: [Average Rating] | Does average Rating differ by Item Fat Content? |

## 3. Page 2: AI Insights (optional)
Get data > MySQL > `agent_insights` (it appears after the agent saves its first insight). Add a **Table** visual with Created At, Question, Answer, and press Refresh after new insights.
