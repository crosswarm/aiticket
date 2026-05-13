import pandas as pd
import os
import re

# Config
CSV_PATH = "/Users/cross/Documents/用友/AI工单/src/工作流-2025完成 (股份Jira) 2026-01-24T22_19_20+0800.csv"
CREW_PATH = "/Users/cross/Documents/用友/AI工单/crewlist.md"
OUTPUT_DIR = "/Users/cross/Documents/用友/AI工单/conclusion"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "Workflow_Analysis_2025.md")

def parse_crew_roles():
    """Parses crewlist.md to get Role -> Name list and Name -> Role mapping."""
    role_map = {} # Name -> Role
    
    current_role = "Unknown"
    if not os.path.exists(CREW_PATH):
        print(f"Warning: Crewlist not found at {CREW_PATH}")
        return role_map
        
    with open(CREW_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line.startswith('## '):
                # e.g. ## 开发
                current_role = line.replace('## ', '').strip()
            elif line.startswith('- '):
                # e.g. - zhangsan, 张三
                clean = line.replace('- ', '')
                parts = re.split(r'[,，]', clean)
                if len(parts) >= 1:
                    username = parts[0].strip()
                    # realname = parts[1].strip() if len(parts) > 1 else username
                    role_map[username] = current_role
    return role_map

def main():
    print("Loading data...")
    df = pd.read_csv(CSV_PATH)
    
    # 1. Preprocessing
    # Map Roles
    role_map = parse_crew_roles()
    
    def get_role(user):
        if pd.isna(user): return "Unknown"
        # user might be "zhangsan" or "张三" depending on CSV. Header says "经办人".
        # let's assume it matches the keys in crewlist (usernames). 
        # If headers are real names, this map might fail if keys are usernames.
        # Let's try to match both key or value from crewlist? 
        # Actually crewlist format is "- username, name". 
        # Let's wait to see data. For now assuming straightforward match.
        return role_map.get(user, "未记录角色")

    df['Role'] = df['经办人'].apply(get_role)
    
    # Target 1: General Stats
    # Fields: Type, Count, Role, Role Count, Role%, Assignee, Assignee Count
    
    type_col = '自定义字段(客户问题类型)'
    
    # A. Type Stats
    # Group by Type -> Count
    type_stats = df[type_col].value_counts().reset_index()
    type_stats.columns = [type_col, '问题数量']
    type_stats['占比'] = (type_stats['问题数量'] / len(df) * 100).map('{:.2f}%'.format)
    
    # B. Role Stats
    role_counts = df['Role'].value_counts()
    role_stats = role_counts.reset_index()
    role_stats.columns = ['处理角色', '角色处理数']
    role_stats['角色处理占比'] = (role_stats['角色处理数'] / len(df) * 100).map('{:.2f}%'.format)
    
    # C. Assignee Stats
    assignee_counts = df['经办人'].value_counts()
    assignee_stats = assignee_counts.reset_index()
    assignee_stats.columns = ['处理人', '处理人处理问题数']
    assignee_stats['处理占比'] = (assignee_stats['处理人处理问题数'] / len(df) * 100).map('{:.2f}%'.format)
    
    # Target 2: Type x Role Pivot
    pivot = pd.crosstab(df[type_col], df['Role'], margins=True, margins_name="Total")
    
    # --- Generate Markdown ---
    print("Generating report...")
    
    md = "# 2025年度工单数据分析报告\n\n"
    md += "本报告基于2025年工作流数据进行多维分析，重点关注问题类型、处理角色及人员投入情况。\n\n"
    
    # 1. General Analysis
    md += "## 1. 总体概况分析\n\n"
    
    md += "### 1.1 客户问题类型分布\n"
    md += "该指标反映了客户反馈的主要问题类别。\n\n"
    md += type_stats.to_markdown(index=False) + "\n\n"
    md += "```mermaid\npie title 客户问题类型分布\n"
    for _, row in type_stats.head(10).iterrows():
        md += f'    "{row[type_col]}" : {row["问题数量"]}\n'
    md += "```\n\n"
    
    md += "### 1.2 处理角色分布\n"
    md += "各职能角色在工单处理中的投入占比。\n\n"
    md += role_stats.to_markdown(index=False) + "\n\n"
    md += "```mermaid\npie title 处理角色分布\n"
    for _, row in role_stats.iterrows():
        md += f'    "{row["处理角色"]}" : {row["角色处理数"]}\n'
    md += "```\n\n"
    
    md += "### 1.3 处理人工作量排行 (Top 20)\n"
    md += "核心处理人员的工作量统计。\n\n"
    md += assignee_stats.head(20).to_markdown(index=False) + "\n\n"

    # 2. Cross Analysis
    md += "## 2. 客户问题类型与处理角色交叉分析\n\n"
    md += "本章节详细拆解每种问题类型的角色构成与核心处理人。\n\n"
    
    # Detailed Breakdown for EACH Type (Top 10 Types by count to avoid clutter, or all?)
    # User asked for analysis, usually implies for all major types. Let's do Top 10.
    top_types = type_stats.head(10)[type_col].tolist()
    
    for t in top_types:
        sub_df = df[df[type_col] == t]
        total_sub = len(sub_df)
        if total_sub == 0: continue
        
        md += f"### [{t}] 详细分析\n"
        md += f"- **总数量**: {total_sub} (占总工单 {total_sub/len(df)*100:.2f}%)\n\n"
        
        # Role Breakdown
        role_sub = sub_df['Role'].value_counts().reset_index()
        role_sub.columns = ['处理角色', '数量']
        role_sub['占比'] = (role_sub['数量'] / total_sub * 100).map('{:.2f}%'.format)
        
        # Assignee Breakdown (Top 1)
        assignee_sub = sub_df['经办人'].value_counts().reset_index()
        assignee_sub.columns = ['处理人', '数量']
        top_assignee = assignee_sub.iloc[0] if not assignee_sub.empty else None
        
        md += "**角色分布**:\n"
        md += role_sub.to_markdown(index=False) + "\n\n"
        
        if top_assignee is not None:
            top_ratio = (top_assignee['数量'] / total_sub * 100)
            md += f"**处理最多的人员**: `{top_assignee['处理人']}`\n"
            md += f"- 处理数量: {top_assignee['数量']}\n"
            md += f"- 该类型占比: {top_ratio:.2f}%\n\n"
            
        # Mermaid
        md += "```mermaid\npie title " + t + " 角色分布\n"
        for _, r in role_sub.iterrows():
             md += f'    "{r["处理角色"]}" : {r["数量"]}\n'
        md += "```\n\n"
        
        md += "---\n\n"

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write(md)
    print(f"Analysis saved to {OUTPUT_FILE}")

    # --- Excel Generaton (Target Layout) ---
    print("Generating Excel...")
    excel_rows = []
    
    # We iterate through all types found
    all_types = df[type_col].value_counts().index.tolist()
    
    for t in all_types:
        sub_df = df[df[type_col] == t]
        total_sub = len(sub_df)
        if total_sub == 0: continue
        
        # 1. Count & Ratio
        ratio_sub = f"{total_sub/len(df)*100:.2f}%"
        
        # 2. Role Ratios
        role_counts_sub = sub_df['Role'].value_counts(normalize=True)
        dev_ratio = role_counts_sub.get('开发', 0.0) * 100
        prod_ratio = role_counts_sub.get('产品经理', 0.0) * 100
        test_ratio = role_counts_sub.get('测试', 0.0) * 100
        
        # 3. Top Assignee (Highest count in this type)
        # Find the max assignee
        assignee_sub_counts = sub_df['经办人'].value_counts()
        if not assignee_sub_counts.empty:
            top_person = assignee_sub_counts.index[0]
            top_count = assignee_sub_counts.iloc[0]
            # Try to get real name if possible from crewlist map? 
            # The map is Name->Role. I don't have Username->Name easily unless I re-parse.
            # Assuming '经办人' is already the display name or close enough.
        else:
            top_person = ""
            top_count = 0

        excel_rows.append({
            "IssueType": t,
            "Count": total_sub,
            "TotalRatio": ratio_sub,
            "DevRatio": f"{dev_ratio:.2f}%" if dev_ratio > 0 else "-",
            "ProdRatio": f"{prod_ratio:.2f}%" if prod_ratio > 0 else "-",
            "TestRatio": f"{test_ratio:.2f}%" if test_ratio > 0 else "-",
            "TopAssignee": top_person,
            "TopCount": top_count
        })

    # Create DF
    xls_df = pd.DataFrame(excel_rows)
    
    # Rename columns to match the complex header structure request
    # Since pandas writes simple headers by default, we can structure it using MultiIndex or formatting later.
    # The user wants specific headers. Let's create a mapped column DF.
    
    final_df = xls_df.rename(columns={
        "IssueType": "自定字段(客户问题类型)",
        "Count": "问题数量",
        "TotalRatio": "总体占比",
        "DevRatio": "开发",
        "ProdRatio": "产品",
        "TestRatio": "测试",
        "TopAssignee": "处理人",
        "TopCount": "处理数"
    })
    
    # Save
    xlsx_path = os.path.join(OUTPUT_DIR, "Workflow_Analysis_2025.xlsx")
    
    # We can use a MultiIndex for the header to match the image style roughly
    # Level 0: [问题类型, 问题数统计, 问题数统计, 处理角色占比, 处理角色占比, 处理角色占比, 最多角色的最高处理人, 最多角色的最高处理人]
    # Level 1: [Keys...]
    
    columns = [
        ("问题类型", "自定字段(客户问题类型)"),
        ("问题数统计", "问题数量"),
        ("问题数统计", "总体占比"),
        ("处理角色占比", "开发"),
        ("处理角色占比", "产品"),
        ("处理角色占比", "测试"),
        ("最多角色的最高处理人", "处理人"),
        ("最多角色的最高处理人", "处理数")
    ]
    final_df.columns = pd.MultiIndex.from_tuples(columns)
    
    final_df.to_excel(xlsx_path)
    print(f"Excel saved to {xlsx_path}")

if __name__ == "__main__":
    main()
