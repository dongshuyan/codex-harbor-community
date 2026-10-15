import Foundation
import SwiftUI

func movedIDs(_ ids:[String],from offsets:IndexSet,to destination:Int)->[String]? {
    guard !offsets.isEmpty,Set(ids).count==ids.count,
          offsets.allSatisfy({ids.indices.contains($0)}),(0...ids.count).contains(destination) else{return nil}
    var result=ids
    result.move(fromOffsets:offsets,toOffset:destination)
    return result==ids ? nil:result
}

struct ParameterState: Codable, Equatable {
    var mode = "auto"
    var manual: [String] = []
    var contextSource = "preset"
    var reasoningSource = "preset"
    var catalogModel = ""
    var reasoningCatalogModel: String? = nil
    var catalogProvider: String? = nil
    var reasoningCatalogProvider: String? = nil
    enum CodingKeys: String, CodingKey {
        case mode, manual
        case contextSource = "context_source", reasoningSource = "reasoning_source", catalogModel = "catalog_model"
        case reasoningCatalogModel = "reasoning_catalog_model"
        case catalogProvider = "catalog_provider", reasoningCatalogProvider = "reasoning_catalog_provider"
    }
    static var legacy: Self { Self(manual:["context","reasoning"],contextSource:"saved",reasoningSource:"saved") }
}

struct DiscoveredParameters: Codable, Equatable {
    var context: Int
    var contextSource: String
    var reasoning: String
    var levels: [String]
    var effort: String
    var effortField: String
    var effortValues: [String:String]
    var reasoningSource: String
    var catalogModel: String
    var reasoningCatalogModel: String? = nil
    var catalogProvider: String? = nil
    var reasoningCatalogProvider: String? = nil
    enum CodingKeys: String, CodingKey {
        case context, reasoning, levels, effort
        case contextSource = "context_source", reasoningSource = "reasoning_source", catalogModel = "catalog_model"
        case effortField = "effort_field", effortValues = "effort_values"
        case reasoningCatalogModel = "reasoning_catalog_model"
        case catalogProvider = "catalog_provider", reasoningCatalogProvider = "reasoning_catalog_provider"
    }
    static var preset: Self { Self(context:128000,contextSource:"preset",reasoning:"off",levels:[],effort:"low",effortField:"reasoning.effort",effortValues:[:],reasoningSource:"preset",catalogModel:"") }
}

extension ModelMapping {
    mutating func markManual(_ field: String) {
        if !parameters.manual.contains(field) { parameters.manual.append(field); parameters.manual.sort() }
        if field == "context" { parameters.contextSource = "manual" } else { parameters.reasoningSource = "manual" }
    }
    mutating func apply(_ value: DiscoveredParameters) {
        if !parameters.manual.contains("context") {
            context=value.context; parameters.contextSource=value.contextSource; parameters.catalogModel=value.catalogModel
            parameters.catalogProvider=value.catalogProvider
        }
        if !parameters.manual.contains("reasoning") {
            reasoning=value.reasoning; levels=value.levels; effort=value.effort
            effortField=value.effortField; effortValues=value.effortValues; parameters.reasoningSource=value.reasoningSource
            parameters.reasoningCatalogModel=value.reasoningCatalogModel
            parameters.reasoningCatalogProvider=value.reasoningCatalogProvider
        }
    }
    mutating func usePreset() { apply(.preset) }
}

func parseContext(_ raw: String) -> Int? {
    var text=raw.precomposedStringWithCompatibilityMapping.trimmingCharacters(in:.whitespacesAndNewlines).lowercased()
    text=text.replacingOccurrences(of:#"\s*(tokens?|词元)\s*$"#,with:"",options:.regularExpression)
        .replacingOccurrences(of:#"[,\s_]"#,with:"",options:.regularExpression)
    guard text.range(of:#"^\d+(?:\.\d+)?[km]?$"#,options:.regularExpression) != nil else { return nil }
    var factor=Decimal(1)
    if text.hasSuffix("k") { factor=1000; text.removeLast() }
    else if text.hasSuffix("m") { factor=1000000; text.removeLast() }
    guard let value=Decimal(string:text,locale:Locale(identifier:"en_US_POSIX")) else{return nil}
    var number=value*factor;var integral=Decimal()
    NSDecimalRound(&integral,&number,0,.plain)
    guard integral==number,number>=4096,number<=2000000 else{return nil}
    return NSDecimalNumber(decimal:number).intValue
}

struct ContextInputState {
    var raw: String?
    func text(context:Int)->String {raw ?? String(context)}
    mutating func edit(_ text:String)->Int {raw=text;return parseContext(text) ?? 0}
    mutating func finish(context:Int) {if context>0 {raw=nil}}
}

func reorderedIDs(_ ids: [String], moving: String, to target: String) -> [String]? {
    guard moving != target, let from=ids.firstIndex(of:moving), let to=ids.firstIndex(of:target) else { return nil }
    var next=ids; next.remove(at:from); next.insert(moving,at:to); return next
}

struct DiscoveryResponse: Codable {
    var status: String
    var httpStatus: Int?
    var models: [String:DiscoveredParameters]
    enum CodingKeys: String, CodingKey { case status, models; case httpStatus="http_status" }
    var message: String {
        switch status {
        case "ok":
            let found=models.values.contains{$0.contextSource != "preset" || $0.reasoningSource != "preset"}
            return found ? "已完成获取。可识别的信息已填入；缺少的信息保留通用预设，手动修改保持不变。" : "已完成获取，但没有找到这些模型的参数。保留通用预设和手动修改，可自行调整。"
        case "http_error": return "Router 未提供可读取的信息（HTTP \(httpStatus ?? 0)）。已检查本机参考目录；这不代表模型测活失败。"
        case "conflicting_metadata": return "已完成获取。当前 API 返回了冲突的同名模型信息；冲突模型保留通用预设，不自动套用模型库。其他模型正常补齐，手动设置保持不变。"
        case "timeout": return "获取已超时（12 秒）。已检查本机参考目录；没有发送模型请求。"
        default: return "未能读取 Router 的模型信息。已检查本机参考目录；没有发送模型请求。"
        }
    }
}

@MainActor final class MetadataController: ObservableObject {
    @Published var loading=false
    @Published var message: String?
    @Published var response: DiscoveryResponse?
    private var generation=0
    private let invoke: ([String:Any]) async throws -> Data
    init(invoke: @escaping ([String:Any]) async throws -> Data = Engine.call) { self.invoke=invoke }
    func invalidate() { generation += 1; loading=false; response=nil; message=nil }
    func load(_ profile: Profile, key:String, names:[String], keyURL:String = "") async -> DiscoveryResponse? {
        generation += 1; let current=generation; loading=true; message=nil
        do {
            var object=try JSONSerialization.jsonObject(with:JSONEncoder().encode(profile)) as! [String:Any]
            object["key_url"]=keyURL
            let data=try await invoke(["action":"discover_models","profile":object,"api_key":key,"models":names])
            guard generation==current, !Task.isCancelled else { return nil }
            let result=try JSONDecoder().decode(DiscoveryResponse.self,from:data)
            response=result;message=result.message;loading=false;return result
        } catch {
            guard generation==current, !Task.isCancelled else { return nil }
            message=(error as? EngineError)?.message ?? "获取未完成，请检查连接信息。";loading=false;return nil
        }
    }
}
