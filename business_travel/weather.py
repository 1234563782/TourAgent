"""Present weather observations without model-added numeric or condition claims."""


def render_weather(data):
    if data.get("status") != "ok":
        return "天气查询未取得有效数据，不能确认天气情况，请稍后重试。"
    forecasts = data.get("forecasts")
    if not isinstance(forecasts, list) or not forecasts:
        return "天气查询未取得有效预报，不能确认天气情况。"
    lines = [f"天气预报（来源：{data.get('source') or '未标注'}；以供应商数据为准）："]
    for forecast in forecasts:
        if not isinstance(forecast, dict):
            continue
        lines.append(f"\n日期：{forecast.get('date') or data.get('date') or '待核实'}")
        lines.append(f"气温：{forecast.get('mintempC', '未知')} 至 "
                     f"{forecast.get('maxtempC', '未知')} 摄氏度。")
        hours = forecast.get("hourly")
        for hour in hours if isinstance(hours, list) else []:
            if not isinstance(hour, dict):
                continue
            raw_time = str(hour.get("time", ""))
            time_label = (raw_time.zfill(4)[:2] + ":" + raw_time.zfill(4)[2:]
                          if raw_time.isdigit() and len(raw_time) <= 4 else "时刻待核实")
            descriptions = hour.get("weatherDesc")
            description = "、".join(str(item["value"]).strip() for item in descriptions
                                   if isinstance(item, dict) and item.get("value")) \
                if isinstance(descriptions, list) else ""
            lines.append(f"- {time_label}：{hour.get('tempC', '未知')} 摄氏度；"
                         f"风速 {hour.get('windspeedKmph', '未知')} 千米/小时；"
                         f"降雨概率 {hour.get('chanceofrain', '未知')}%；"
                         f"天气原文：{description or '未知'}。")
    lines.append("\n预报不保证实际天气；未将风速换算成风力等级。")
    return "\n".join(lines)
