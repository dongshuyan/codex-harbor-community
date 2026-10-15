import SwiftUI

struct ModelParameterEditor: View {
    @Binding var mapping: ModelMapping
    var refresh: () -> Void
    let loading: Bool
    @State private var contextInput=ContextInputState()
    @FocusState private var editingContext: Bool
    private let levels=["none","minimal","low","medium","high","xhigh","max"]
    private let fields=["reasoning.effort","reasoning.level","reasoning_effort","thinking_level"]

    var body: some View {
        VStack(alignment:.leading,spacing:15) {
            HStack {
                Text("模型参数").font(.headline)
                Spacer()
                if loading { ProgressView().controlSize(.small) }
                Button("重新获取",action:refresh).accessibilityLabel("重新获取参数 " + mapping.alias).disabled(loading || mapping.upstream.trimmingCharacters(in:.whitespaces).isEmpty || mapping.parameters.mode != "auto")
            }
            Picker("参数来源",selection:Binding(get:{mapping.parameters.mode},set:{ mode in
                mapping.parameters.mode=mode;mapping.usePreset()
            })) {
                Text("自动获取（优先）").tag("auto")
                Text("Harbor 通用预设").tag("preset")
            }.pickerStyle(.segmented)
            Text(mapping.parameters.mode == "auto" ? "读取当前 API 的信息；缺少时按实际 ModelName 匹配 Cherry 模型库等本机目录。不会覆盖手动修改。" : "未知模型使用 128000 上下文与服务默认推理；这是可修改的预设，并非服务商确认的能力。")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal:false,vertical:true)
            Divider()
            VStack(alignment:.leading,spacing:7) {
                HStack {
                    VStack(alignment:.leading,spacing:4) {
                        Text("上下文窗口").font(.callout.weight(.medium))
                        Text(source(mapping.parameters.contextSource,provider:mapping.parameters.catalogProvider)).font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    TextField("128000",text:Binding(get:{contextInput.text(context:mapping.context)},set:{text in
                        mapping.context=contextInput.edit(text);mapping.markManual("context")
                    })).textFieldStyle(.roundedBorder).frame(width:150)
                        .focused($editingContext).accessibilityLabel("上下文 token 上限")
                        .onSubmit {contextInput.finish(context:mapping.context)}
                    Text("token").font(.caption).foregroundStyle(.secondary)
                }
                if mapping.context==0 { Text("请输入 4096–2000000；支持 1,050,000、1.05M 等写法。").font(.caption).foregroundStyle(.orange) }
                if mapping.parameters.contextSource == "catalog" && mapping.parameters.catalogProvider != "cherry" {
                    Text("参考：\(mapping.parameters.catalogModel) · 公开目录 2026-10-15。Router 的实际限制可能不同。").font(.caption).foregroundStyle(.secondary)
                }
                if mapping.parameters.contextSource == "catalog" && mapping.parameters.catalogProvider == "cherry" {
                    Text("参考：\(mapping.parameters.catalogModel) · Cherry Studio 2.0.14 模型库。当前 API 的实际限制可能不同。")
                        .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal:false,vertical:true)
                }
                restoreButton("context")
            }
            Divider()
            VStack(alignment:.leading,spacing:10) {
                HStack { Text("推理程度").font(.callout.weight(.medium));Spacer();Text(source(mapping.parameters.reasoningSource,provider:mapping.parameters.reasoningCatalogProvider)).font(.caption).foregroundStyle(.secondary) }
                if mapping.parameters.reasoningSource == "catalog" && mapping.parameters.reasoningCatalogProvider == "cherry" {
                    Text(mapping.reasoning == "off"
                         ? "参考：\(mapping.parameters.reasoningCatalogModel ?? "") · 目录需要思考开关或预算适配，当前保留服务默认值。"
                         : "参考：\(mapping.parameters.reasoningCatalogModel ?? "") · 按标准 Responses 发送。尚未对当前 API 逐档验证，可自行调整。")
                        .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal:false,vertical:true)
                }
                Picker("如何发送推理参数",selection:manual(\.reasoning)) {
                    Text("服务默认值（不传参数）").tag("off")
                    Text("标准 Responses 程度").tag("standard")
                    Text("自定义字段或取值").tag("custom")
                    if mapping.reasoning == "forward" { Text("已保存的程度").tag("forward") }
                }
                if mapping.reasoning == "off" {
                    Text("尚未确认字段与档位时，不发送推理参数。服务端仍可能自行启用推理。").font(.caption).foregroundStyle(.secondary)
                } else {
                    LazyVGrid(columns:[GridItem(.adaptive(minimum:82),alignment:.leading)],alignment:.leading,spacing:10) {
                        ForEach(levels,id:\.self) { level in
                            Toggle(level == "max" ? "Max" : level,isOn:Binding(get:{mapping.levels.contains(level)},set:{ enabled in
                                mapping.markManual("reasoning")
                                if enabled {mapping.levels=levels.filter{mapping.levels.contains($0) || $0==level};mapping.effortValues[level]=level}
                                else {mapping.levels.removeAll{$0==level};mapping.effortValues.removeValue(forKey:level)}
                                if !mapping.levels.contains(mapping.effort){mapping.effort=mapping.levels.first ?? "low"}
                            })).toggleStyle(.checkbox)
                        }
                    }
                    if !mapping.levels.isEmpty { Picker("默认程度",selection:manual(\.effort)){ForEach(mapping.levels,id:\.self){Text($0).tag($0)}} }
                    else { Text("请选择至少一个服务商支持的程度，或使用服务默认值。").font(.caption).foregroundStyle(.orange) }
                    if mapping.reasoning == "custom" {
                        DisclosureGroup("高级设置 · 上游字段与取值") {
                            VStack(alignment:.leading,spacing:10) {
                                Text("“上游字段”就是 API 接收推理设置的参数名；例如 reasoning.effort。只有服务商明确说明不同写法时才修改。").font(.caption).foregroundStyle(.secondary)
                                Picker("上游字段",selection:manual(\.effortField)){ForEach(fields,id:\.self){Text($0).tag($0)}}
                                ForEach(mapping.levels,id:\.self){level in
                                    HStack { Text("Codex "+level).frame(width:110,alignment:.leading);Image(systemName:"arrow.right").foregroundStyle(.secondary)
                                        TextField("实际取值",text:Binding(get:{mapping.effortValues[level] ?? level},set:{mapping.effortValues[level]=$0;mapping.markManual("reasoning")})).textFieldStyle(.roundedBorder)
                                    }
                                }
                            }.padding(.top,8)
                        }
                    }
                }
                restoreButton("reasoning")
            }
        }
        .onChange(of:mapping.context) { _, value in if !editingContext {contextInput.finish(context:value)} }
        .onChange(of:editingContext) { _, active in if !active {contextInput.finish(context:mapping.context)} }
    }
    func source(_ value:String,provider:String? = nil)->String {
        switch value {
        case "router":return "来自当前 API"
        case "catalog":return provider == "cherry" ? "Cherry 模型库参考" : "公开目录参考"
        case "manual":return "手动设置 · 自动获取不会覆盖"
        case "saved":return "原有设置 · 已保护"
        default:return "Harbor 通用预设"
        }
    }
    func manual(_ path:WritableKeyPath<ModelMapping,String>)->Binding<String> {
        Binding(get:{mapping[keyPath:path]},set:{value in
            mapping[keyPath:path]=value;mapping.markManual("reasoning")
            if path == \.reasoning {
                if value=="off" {mapping.levels=[];mapping.effortValues=[:]}
                if value=="standard" {mapping.effortField="reasoning.effort";mapping.effortValues=Dictionary(uniqueKeysWithValues:mapping.levels.map{($0,$0)})}
            }
        })
    }
    @ViewBuilder func restoreButton(_ field:String)->some View {
        if mapping.parameters.manual.contains(field) {
            Button(mapping.parameters.mode=="auto" ? "恢复此项自动获取" : "恢复此项通用预设") {
                mapping.parameters.manual.removeAll{$0==field};mapping.usePreset()
                if mapping.parameters.mode=="auto"{refresh()}
            }.font(.caption).buttonStyle(.link).disabled(loading)
        }
    }
}
