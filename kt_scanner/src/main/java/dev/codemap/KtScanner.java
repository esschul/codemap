package dev.codemap;

import org.treesitter.*;

import java.io.*;
import java.nio.file.*;
import java.util.*;
import java.util.function.Consumer;

/**
 * Reads .kt file paths from stdin (one per line), outputs a JSON array to stdout.
 *
 * Output shape matches the Java ast-scanner:
 * {
 *   "file": "...",
 *   "className": "...",
 *   "annotations": ["Service", ...],
 *   "fields": [{"name": "repo", "type": "OrderRepository"}, ...],
 *   "methods": [
 *     {
 *       "name": "getOrder",
 *       "annotations": ["GetMapping"],
 *       "mappingPath": "/orders/{id}",
 *       "callsOnFields": ["repo"]
 *     }
 *   ]
 * }
 */
public class KtScanner {

    private static final Set<String> SKIP_TYPES = new HashSet<>(Arrays.asList(
        "String", "Int", "Long", "Boolean", "Double", "Float", "Short", "Byte", "Char",
        "Unit", "Any", "Nothing", "Number",
        "List", "MutableList", "Set", "MutableSet", "Map", "MutableMap",
        "Collection", "Iterable", "Sequence", "Array",
        "Optional", "Result",
        "ObjectMapper", "Logger", "Duration", "Instant",
        "LocalDate", "LocalDateTime", "ZonedDateTime",
        "BigDecimal", "BigInteger", "UUID",
        "HttpServletRequest", "HttpServletResponse",
        "T", "R", "K", "V", "E", "A", "B"
    ));

    private static final Set<String> MAPPING_ANNOTATIONS = new HashSet<>(Arrays.asList(
        "GetMapping", "PostMapping", "PutMapping", "DeleteMapping", "PatchMapping", "RequestMapping"
    ));

    public static void main(String[] args) throws Exception {
        TSLanguage kotlin = new TreeSitterKotlin();
        TSParser parser = new TSParser();
        parser.setLanguage(kotlin);

        BufferedReader reader = new BufferedReader(new InputStreamReader(System.in));
        List<String> results = new ArrayList<>();

        String line;
        while ((line = reader.readLine()) != null) {
            String path = line.trim();
            if (path.isEmpty()) continue;
            try {
                String source = new String(Files.readAllBytes(Paths.get(path)));
                TSTree tree = parser.parseString(null, source);
                byte[] srcBytes = source.getBytes("UTF-8");
                String json = scanFile(path, tree.getRootNode(), srcBytes);
                if (json != null) results.add(json);
            } catch (Exception e) {
                System.err.println("WARN: could not parse " + path + ": " + e.getMessage());
            }
        }

        System.out.print("[" + String.join(",", results) + "]");
    }

    private static String scanFile(String filePath, TSNode root, byte[] src) {
        // Find first class or object declaration (skip companion objects)
        TSNode classNode = findFirst(root, "class_declaration", "object_declaration");
        String className = classNode != null
                ? text(findFirstChild(classNode, "type_identifier"), src) : null;
        List<String> classAnnotations = classNode != null
                ? collectAnnotations(classNode, src) : new ArrayList<>();

        // Fallback: tree-sitter 0.3.x misparses multi-annotation class declarations
        // (2+ annotations before the class keyword) as nested prefix_expression chains
        // rather than class_declaration. The pattern is:
        //   prefix_expression(annotation, prefix_expression(annotation, infix_expression("class" Name)))
        // We detect and recover from this by finding the outermost prefix_expression at
        // source_file level that wraps something containing the "class" keyword.
        if (className == null) {
            TSNode misparsed = findMisparsedClassPrefixNode(root, src);
            if (misparsed != null) {
                className = extractClassNameFromMisparsed(misparsed, src);
                classAnnotations = extractAnnotationsFromMisparsed(misparsed, src);
                classNode = misparsed; // use as anchor for constructor sibling search
            }
        }

        if (className == null) return null;

        // Primary constructor parameters → injected fields.
        // Standard: class Foo(val x: Bar) → primary_constructor is a direct child.
        // Annotated form: class Foo \n @Autowired \n constructor(val x: Bar) { ... }
        //   tree-sitter parses this as a sibling call_expression(constructor, value_args)
        //   with the class body as an annotated_lambda inside the call_suffix.
        Map<String, String> fieldMap = new LinkedHashMap<>();
        TSNode primaryCtor = findFirstChild(classNode, "primary_constructor");
        // ctorCallExpr: the outer call_expression for the @Autowired constructor case
        TSNode ctorCallExpr = null;

        if (primaryCtor != null) {
            forEachChild(primaryCtor, "class_parameter", param -> {
                String name = text(findFirstChild(param, "simple_identifier"), src);
                String type = simpleType(findUserType(param), src);
                if (name != null && type != null && !SKIP_TYPES.contains(type)) {
                    fieldMap.put(name, type);
                }
            });
        } else {
            ctorCallExpr = findAnnotatedConstructorSibling(root, classNode, src);
            if (ctorCallExpr != null) {
                extractAnnotatedConstructorParams(ctorCallExpr, src, fieldMap);
            }
        }

        // Body-declared val/var with @Autowired / @Inject
        // Standard: class_body is a direct child of class_declaration.
        // Annotated constructor: body is parsed as lambda_literal inside the call_suffix.
        TSNode body = findFirstChild(classNode, "class_body");
        if (body == null && ctorCallExpr != null) {
            body = findLambdaBodyInCtorCall(ctorCallExpr);
        }
        if (body != null) {
            forEachChild(body, "property_declaration", prop -> {
                List<String> anns = collectAnnotations(prop, src);
                if (anns.contains("Autowired") || anns.contains("Inject")) {
                    String name = text(findFirstChild(prop, "simple_identifier"), src);
                    String type = simpleType(findUserType(prop), src);
                    if (name != null && type != null && !SKIP_TYPES.contains(type)) {
                        fieldMap.put(name, type);
                    }
                }
            });
        }

        if (fieldMap.isEmpty() && classAnnotations.isEmpty()) return null;

        // Pass 1: collect direct field calls and private-method calls for every function.
        // methodDirectFields:  fnName → fields called directly in that function body
        // methodCallsPrivate:  fnName → private sibling functions called directly
        Map<String, List<String>> methodDirectFields = new LinkedHashMap<>();
        Map<String, List<String>> methodCallsPrivate = new LinkedHashMap<>();

        // methodDirectFieldCalls: fnName → ordered {field,type,method} records (direct only, for evidence packets)
        Map<String, List<Map<String, String>>> methodDirectFieldCalls = new LinkedHashMap<>();

        if (body != null) {
            // Only private methods are eligible for transitive expansion.
            // Following public sibling calls would give misleading chains in controllers
            // where public endpoint methods happen to call each other.
            Set<String> privateMethodNames = new LinkedHashSet<>();
            forEachFunction(body, fn -> {
                if (isPrivate(fn, src)) {
                    String n = text(findFirstChild(fn, "simple_identifier"), src);
                    if (n != null) privateMethodNames.add(n);
                }
            });

            forEachFunction(body, fn -> {
                String fnName = text(findFirstChild(fn, "simple_identifier"), src);
                if (fnName == null) return;

                // Collect {field, type, method} tuples for direct field calls in this function.
                List<Map<String, String>> fieldCalls = collectFieldCalls(fn, fieldMap, src);
                methodDirectFieldCalls.put(fnName, fieldCalls);

                // Derive the plain field-name list for existing transitive expansion logic.
                List<String> directFields = new ArrayList<>();
                Set<String> seen = new LinkedHashSet<>();
                for (Map<String, String> fc : fieldCalls) {
                    if (seen.add(fc.get("field"))) directFields.add(fc.get("field"));
                }
                methodDirectFields.put(fnName, directFields);

                // Collect calls to private sibling methods only
                List<String> helperCalls = new ArrayList<>();
                for (String sibling : privateMethodNames) {
                    if (!sibling.equals(fnName) && subtreeContainsLocalCall(fn, sibling, src)) {
                        helperCalls.add(sibling);
                    }
                }
                methodCallsPrivate.put(fnName, helperCalls);
            });
        }

        // Pass 2: expand both callsOnFields and fieldCalls transitively through private helpers.
        // Dedup callsOnFields by field name; dedup fieldCalls by (field, method) pair so that
        // "checkoutService.validate()" and "checkoutService.createSession()" are both preserved.
        Map<String, List<String>> methodAllFields = new LinkedHashMap<>();
        Map<String, List<Map<String, String>>> methodAllFieldCalls = new LinkedHashMap<>();
        for (String fnName : methodDirectFields.keySet()) {
            Set<String> visited = new LinkedHashSet<>();
            Set<String> fieldNames = new LinkedHashSet<>(methodDirectFields.get(fnName));
            Set<String> fcSeen = new LinkedHashSet<>();
            List<Map<String, String>> fcList = new ArrayList<>();

            for (Map<String, String> fc : methodDirectFieldCalls.getOrDefault(fnName, List.of())) {
                if (fcSeen.add(fc.get("field") + "\0" + fc.get("method"))) fcList.add(fc);
            }

            Queue<String> queue = new ArrayDeque<>(methodCallsPrivate.getOrDefault(fnName, List.of()));
            while (!queue.isEmpty()) {
                String callee = queue.poll();
                if (!visited.add(callee)) continue;
                fieldNames.addAll(methodDirectFields.getOrDefault(callee, List.of()));
                for (Map<String, String> fc : methodDirectFieldCalls.getOrDefault(callee, List.of())) {
                    if (fcSeen.add(fc.get("field") + "\0" + fc.get("method"))) fcList.add(fc);
                }
                queue.addAll(methodCallsPrivate.getOrDefault(callee, List.of()));
            }
            methodAllFields.put(fnName, new ArrayList<>(fieldNames));
            methodAllFieldCalls.put(fnName, fcList);
        }

        // Build method JSON using expanded field call sets.
        List<String> methodJsons = new ArrayList<>();
        if (body != null) {
            forEachFunction(body, fn -> {
                String fnName = text(findFirstChild(fn, "simple_identifier"), src);
                if (fnName == null) return;
                List<String> fnAnnotations = collectAnnotations(fn, src);
                String mappingPath = extractMappingPath(fn, src);
                // callsOnFields: transitively expanded field names (existing behaviour)
                List<String> calledFields = methodAllFields.getOrDefault(fnName, List.of());
                // fieldCalls: transitively expanded {field,type,method} tuples for evidence packets.
                // Same BFS scope as callsOnFields, but preserves method names and allows multiple
                // methods per field (e.g. service.validate() + service.createSession() both kept).
                List<Map<String, String>> fieldCallDetails =
                        methodAllFieldCalls.getOrDefault(fnName, List.of());
                String fieldCallsJson = fieldCallDetailsJson(fieldCallDetails);
                methodJsons.add(
                    "{\"name\":" + js(fnName) +
                    ",\"annotations\":" + jsList(fnAnnotations) +
                    ",\"mappingPath\":" + (mappingPath != null ? js(mappingPath) : "null") +
                    ",\"callsOnFields\":" + jsList(calledFields) +
                    ",\"fieldCalls\":" + fieldCallsJson + "}"
                );
            });
        }

        StringBuilder fieldsJson = new StringBuilder();
        boolean first = true;
        for (Map.Entry<String, String> e : fieldMap.entrySet()) {
            if (!first) fieldsJson.append(",");
            fieldsJson.append("{\"name\":").append(js(e.getKey()))
                      .append(",\"type\":").append(js(e.getValue())).append("}");
            first = false;
        }

        return "{\"file\":" + js(filePath) +
               ",\"className\":" + js(className) +
               ",\"annotations\":" + jsList(classAnnotations) +
               ",\"fields\":[" + fieldsJson + "]" +
               ",\"methods\":[" + String.join(",", methodJsons) + "]}";
    }

    // ── Annotation extraction ──────────────────────────────────────────────────

    private static List<String> collectAnnotations(TSNode node, byte[] src) {
        List<String> result = new ArrayList<>();
        for (int i = 0; i < node.getChildCount(); i++) {
            TSNode child = node.getChild(i);
            if ("modifiers".equals(child.getType())) {
                for (int j = 0; j < child.getChildCount(); j++) {
                    TSNode ann = child.getChild(j);
                    if ("annotation".equals(ann.getType())) {
                        extractAnnotationNames(ann, src, result);
                    }
                }
            }
        }
        return result;
    }

    private static void extractAnnotationNames(TSNode ann, byte[] src, List<String> result) {
        // annotation → @[use-site-target:]? unescaped_annotation+
        // unescaped_annotation → constructor_invocation → type_reference → user_type → type_identifier
        for (int i = 0; i < ann.getChildCount(); i++) {
            TSNode child = ann.getChild(i);
            String t = child.getType();
            if ("constructor_invocation".equals(t) || "user_type".equals(t)) {
                String name = extractTypeIdentifier(child, src);
                if (name != null) result.add(name);
            }
        }
    }

    /**
     * Extract the simple class name from a type node.
     * Handles qualified names (takes the rightmost segment) and stops before generic
     * type arguments so "KafkaTemplate<String,Any>" → "KafkaTemplate", not "Any".
     * "com.example.order.OrderService" → "OrderService".
     */
    private static String extractTypeIdentifier(TSNode node, byte[] src) {
        List<String> segments = new ArrayList<>();
        collectQualifiedSegments(node, src, segments);
        return segments.isEmpty() ? null : segments.get(segments.size() - 1);
    }

    /**
     * Walk the user_type tree collecting type_identifier segments, but stop descending
     * into type_arguments (generics) so we don't pick up type params as the class name.
     */
    private static void collectQualifiedSegments(TSNode node, byte[] src, List<String> result) {
        String t = node.getType();
        if ("type_identifier".equals(t)) {
            result.add(text(node, src));
            return;
        }
        // Do not recurse into generic type arguments
        if ("type_arguments".equals(t)) return;
        for (int i = 0; i < node.getChildCount(); i++) {
            collectQualifiedSegments(node.getChild(i), src, result);
        }
    }

    // ── Type resolution ────────────────────────────────────────────────────────

    /**
     * Find the user_type child of a node, unwrapping nullable_type if needed.
     * Handles both "val x: Foo" (user_type direct) and "val x: Foo?" (nullable_type → user_type).
     */
    private static TSNode findUserType(TSNode node) {
        TSNode direct = findFirstChild(node, "user_type");
        if (direct != null) return direct;
        TSNode nullable = findFirstChild(node, "nullable_type");
        if (nullable != null) return findFirstChild(nullable, "user_type");
        return null;
    }

    /** Extract the simple (unqualified, non-generic) type name from a user_type or type_reference node. */
    private static String simpleType(TSNode typeNode, byte[] src) {
        if (typeNode == null || typeNode.isNull()) return null;
        return extractTypeIdentifier(typeNode, src);
    }

    // ── Mapping path extraction ────────────────────────────────────────────────

    private static String extractMappingPath(TSNode fn, byte[] src) {
        for (int i = 0; i < fn.getChildCount(); i++) {
            TSNode child = fn.getChild(i);
            if ("modifiers".equals(child.getType())) {
                for (int j = 0; j < child.getChildCount(); j++) {
                    TSNode ann = child.getChild(j);
                    if (!"annotation".equals(ann.getType())) continue;
                    // Find annotation name
                    String annName = null;
                    for (int k = 0; k < ann.getChildCount(); k++) {
                        TSNode ac = ann.getChild(k);
                        if ("constructor_invocation".equals(ac.getType()) || "user_type".equals(ac.getType())) {
                            annName = extractTypeIdentifier(ac, src);
                            break;
                        }
                    }
                    if (annName != null && MAPPING_ANNOTATIONS.contains(annName)) {
                        return extractFirstStringArg(ann, src);
                    }
                }
            }
        }
        return null;
    }

    private static String extractFirstStringArg(TSNode ann, byte[] src) {
        // Look for value_arguments → value_argument → string_literal
        TSNode valueArgs = findFirst(ann, "value_arguments");
        if (valueArgs == null) return null;
        for (int i = 0; i < valueArgs.getChildCount(); i++) {
            TSNode arg = valueArgs.getChild(i);
            if (!"value_argument".equals(arg.getType())) continue;
            TSNode strLit = findFirst(arg, "string_literal");
            if (strLit == null) continue;
            // Collect literal content entries (skip quotes, template markers)
            StringBuilder sb = new StringBuilder();
            for (int j = 0; j < strLit.getChildCount(); j++) {
                TSNode entry = strLit.getChild(j);
                if ("string_content".equals(entry.getType()) || "line_str_text".equals(entry.getType())) {
                    sb.append(text(entry, src));
                }
            }
            String result = sb.toString();
            if (result.isEmpty()) {
                // Fallback: strip surrounding quotes from raw text
                String raw = text(strLit, src);
                if (raw.startsWith("\"") && raw.endsWith("\"") && raw.length() >= 2) {
                    return raw.substring(1, raw.length() - 1);
                }
            }
            return result;
        }
        return null;
    }

    /** Returns true if the function_declaration node has a 'private' visibility modifier. */
    private static boolean isPrivate(TSNode fn, byte[] src) {
        TSNode mods = findFirstChild(fn, "modifiers");
        if (mods == null) return false;
        for (int i = 0; i < mods.getChildCount(); i++) {
            TSNode mod = mods.getChild(i);
            if ("visibility_modifier".equals(mod.getType())
                    && "private".equals(text(mod, src))) {
                return true;
            }
        }
        return false;
    }

    // ── Field call detection ───────────────────────────────────────────────────

    /**
     * Walk a function subtree and collect every direct call on an injected field.
     * Returns ordered {field, type, method} records, deduplicating by (field, method) pair.
     * Only captures calls whose receiver resolves unambiguously to an injected field
     * (direct: service.foo(), or this-qualified: this.service.foo()).
     */
    private static List<Map<String, String>> collectFieldCalls(
            TSNode node, Map<String, String> fieldMap, byte[] src) {
        List<Map<String, String>> result = new ArrayList<>();
        Set<String> seen = new LinkedHashSet<>();
        collectFieldCallsInto(node, fieldMap, src, result, seen);
        return result;
    }

    private static void collectFieldCallsInto(TSNode node, Map<String, String> fieldMap,
            byte[] src, List<Map<String, String>> result, Set<String> seen) {
        if ("call_expression".equals(node.getType()) && node.getChildCount() > 0) {
            TSNode callee = node.getChild(0);
            if ("navigation_expression".equals(callee.getType())) {
                String field = resolveFieldReceiver(callee, fieldMap, src);
                if (field != null) {
                    String method = navigationSelector(callee, src);
                    if (method != null) {
                        String key = field + "." + method;
                        if (seen.add(key)) {
                            Map<String, String> fc = new LinkedHashMap<>();
                            fc.put("field", field);
                            fc.put("type", fieldMap.get(field));
                            fc.put("method", method);
                            result.add(fc);
                        }
                    }
                }
            }
        }
        for (int i = 0; i < node.getChildCount(); i++) {
            collectFieldCallsInto(node.getChild(i), fieldMap, src, result, seen);
        }
    }

    /**
     * Resolve the receiver of a navigation_expression to an injected field name, or null.
     * Matches: service.foo() and this.service.foo() — not wrapper.service.foo().
     */
    private static String resolveFieldReceiver(TSNode navExpr, Map<String, String> fieldMap, byte[] src) {
        if (navExpr.getChildCount() == 0) return null;
        TSNode receiver = navExpr.getChild(0);
        // service.foo()
        if ("simple_identifier".equals(receiver.getType())) {
            String name = text(receiver, src);
            return fieldMap.containsKey(name) ? name : null;
        }
        // this.service.foo()
        if ("navigation_expression".equals(receiver.getType())) {
            TSNode innerReceiver = receiver.getChildCount() > 0 ? receiver.getChild(0) : null;
            if (innerReceiver != null && "this_expression".equals(innerReceiver.getType())) {
                String selector = navigationSelector(receiver, src);
                return (selector != null && fieldMap.containsKey(selector)) ? selector : null;
            }
        }
        return null;
    }

    private static String fieldCallDetailsJson(List<Map<String, String>> fieldCalls) {
        if (fieldCalls.isEmpty()) return "[]";
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < fieldCalls.size(); i++) {
            if (i > 0) sb.append(",");
            Map<String, String> fc = fieldCalls.get(i);
            sb.append("{\"field\":").append(js(fc.get("field")))
              .append(",\"type\":").append(js(fc.get("type")))
              .append(",\"method\":").append(js(fc.get("method"))).append("}");
        }
        return sb.append("]").toString();
    }

    private static boolean subtreeContainsFieldCall(TSNode node, String fieldName, byte[] src) {
        String t = node.getType();
        if ("navigation_expression".equals(t)) {
            TSNode receiver = node.getChildCount() > 0 ? node.getChild(0) : null;
            if (receiver != null) {
                // service.foo() — receiver is simple_identifier [service]
                if ("simple_identifier".equals(receiver.getType())
                        && fieldName.equals(text(receiver, src))) {
                    return true;
                }
                // this.service.foo() — receiver is navigation_expression(this, .service).
                // Only match when the innermost receiver is the 'this' keyword, so that
                // wrapper.service.foo() (where wrapper is NOT 'this') is not falsely counted.
                if ("navigation_expression".equals(receiver.getType())) {
                    TSNode innerReceiver = receiver.getChildCount() > 0 ? receiver.getChild(0) : null;
                    boolean receiverIsThis = innerReceiver != null
                            && "this_expression".equals(innerReceiver.getType());
                    if (receiverIsThis) {
                        String selector = navigationSelector(receiver, src);
                        if (fieldName.equals(selector)) return true;
                    }
                }
            }
        }
        for (int i = 0; i < node.getChildCount(); i++) {
            if (subtreeContainsFieldCall(node.getChild(i), fieldName, src)) return true;
        }
        return false;
    }

    // ── Multi-annotation fallback ──────────────────────────────────────────────

    /**
     * Find a source_file-level prefix_expression that wraps a misparsed class declaration.
     * tree-sitter 0.3.x produces: prefix_expression(ann, prefix_expression(ann, infix_expression))
     * when 2+ annotations precede "internal class Foo". We detect this by checking if the
     * prefix_expression's subtree contains a simple_identifier with text "class".
     */
    private static TSNode findMisparsedClassPrefixNode(TSNode root, byte[] src) {
        for (int i = 0; i < root.getChildCount(); i++) {
            TSNode child = root.getChild(i);
            if ("prefix_expression".equals(child.getType())
                    && subtreeContainsClassKeyword(child, src)) {
                return child;
            }
        }
        return null;
    }

    private static boolean subtreeContainsClassKeyword(TSNode node, byte[] src) {
        if ("simple_identifier".equals(node.getType()) && "class".equals(text(node, src))) return true;
        String t = node.getType();
        // Do NOT descend into annotation arguments — Foo::class inside @Schema(implementation=Foo::class)
        // would otherwise match "class" as a simple_identifier in the misparsed tree.
        if ("value_arguments".equals(t) || "value_argument".equals(t)
                || "constructor_invocation".equals(t) || "class_literal".equals(t)) {
            return false;
        }
        for (int i = 0; i < node.getChildCount(); i++) {
            if (subtreeContainsClassKeyword(node.getChild(i), src)) return true;
        }
        return false;
    }

    /**
     * Extract the class name from a misparsed prefix_expression.
     * Finds the deepest infix_expression and returns the identifier after "class".
     */
    private static String extractClassNameFromMisparsed(TSNode prefixNode, byte[] src) {
        TSNode infixExpr = findFirst(prefixNode, "infix_expression");
        if (infixExpr == null) return null;
        boolean seenClass = false;
        for (int i = 0; i < infixExpr.getChildCount(); i++) {
            TSNode child = infixExpr.getChild(i);
            if ("simple_identifier".equals(child.getType())) {
                String t = text(child, src);
                if (seenClass) return t; // identifier immediately after "class"
                if ("class".equals(t)) seenClass = true;
            }
        }
        return null;
    }

    /**
     * Collect annotations from a misparsed prefix_expression chain.
     * Recurses into nested prefix_expression nodes and collects all annotation children.
     */
    private static List<String> extractAnnotationsFromMisparsed(TSNode prefixNode, byte[] src) {
        List<String> result = new ArrayList<>();
        collectAnnotationsFromPrefixChain(prefixNode, src, result);
        return result;
    }

    private static void collectAnnotationsFromPrefixChain(TSNode node, byte[] src, List<String> result) {
        String t = node.getType();
        if ("annotation".equals(t)) {
            extractAnnotationNames(node, src, result);
            return;
        }
        // Only recurse into prefix_expression chains — stop at infix_expression (the class body)
        if ("prefix_expression".equals(t)) {
            for (int i = 0; i < node.getChildCount(); i++) {
                collectAnnotationsFromPrefixChain(node.getChild(i), src, result);
            }
        }
    }

    /**
     * Walk root's children looking for a call_expression that follows classNode and
     * whose inner expression starts with "constructor". Returns the outer call_expression.
     */
    private static TSNode findAnnotatedConstructorSibling(TSNode root, TSNode classNode, byte[] src) {
        boolean seenClass = false;
        for (int i = 0; i < root.getChildCount(); i++) {
            TSNode child = root.getChild(i);
            if (!seenClass) {
                if (child.getStartByte() == classNode.getStartByte()) seenClass = true;
                continue;
            }
            String t = child.getType();
            if ("class_declaration".equals(t) || "object_declaration".equals(t)
                    || "function_declaration".equals(t)) break;
            if ("call_expression".equals(t)) {
                // Inner call_expression has simple_identifier "constructor"
                TSNode inner = findFirstChild(child, "call_expression");
                if (inner == null) inner = child;
                TSNode fnId = findFirstChild(inner, "simple_identifier");
                if (fnId != null && "constructor".equals(text(fnId, src))) return child;
            }
        }
        return null;
    }

    /**
     * Extract constructor params from the annotated-constructor call_expression.
     *
     * Tree-sitter produces two distinct layouts depending on whether the visibility
     * modifier is present:
     *
     * (a) "val name: Type"  — type lands inside the value_argument as the last simple_identifier
     *     after an ERROR ":".
     *     value_argument → infix_expression → [val, name, ERROR(:), Type]
     *
     * (b) "private val name" followed by ": Type" — the type is in a separate ERROR sibling
     *     immediately after the value_argument at the value_arguments level.
     *     value_argument → infix_expression → [private, val, name]
     *     ERROR → [: Type]
     *
     * We handle both by: after stripping keywords, if the value_argument has ≥2 identifiers
     * the last is the type (case a). If only 1 identifier, the type comes from the next
     * ERROR sibling (case b).
     */
    private static void extractAnnotatedConstructorParams(
            TSNode ctorCallExpr, byte[] src, Map<String, String> fieldMap) {
        TSNode valueArgs = findFirst(ctorCallExpr, "value_arguments");
        if (valueArgs == null) return;

        int count = valueArgs.getChildCount();
        for (int i = 0; i < count; i++) {
            TSNode child = valueArgs.getChild(i);
            if (!"value_argument".equals(child.getType())) continue;

            List<String> ids = new ArrayList<>();
            collectSimpleIdentifiers(child, src, ids);
            ids.removeIf(id -> id.equals("val") || id.equals("var")
                    || id.equals("private") || id.equals("internal")
                    || id.equals("protected") || id.equals("public") || id.equals("override"));

            String name = ids.isEmpty() ? null : ids.get(0);
            String type = null;

            if (ids.size() >= 2) {
                // Case (a): type is the last identifier inside value_argument
                type = ids.get(ids.size() - 1);
            } else {
                // Case (b): look for an ERROR sibling immediately after this value_argument
                for (int j = i + 1; j < count; j++) {
                    TSNode sib = valueArgs.getChild(j);
                    if ("ERROR".equals(sib.getType())) {
                        List<String> errIds = new ArrayList<>();
                        collectSimpleIdentifiers(sib, src, errIds);
                        if (!errIds.isEmpty()) type = errIds.get(errIds.size() - 1);
                        break;
                    }
                    if ("value_argument".equals(sib.getType())) break; // next param, stop
                }
            }

            if (name != null && type != null && !name.equals(type) && !SKIP_TYPES.contains(type)) {
                fieldMap.put(name, type);
            }
        }
    }

    /** The class body in the @Autowired constructor pattern is a lambda_literal inside call_suffix. */
    private static TSNode findLambdaBodyInCtorCall(TSNode ctorCallExpr) {
        // call_expression → call_suffix → annotated_lambda → lambda_literal
        for (int i = 0; i < ctorCallExpr.getChildCount(); i++) {
            TSNode child = ctorCallExpr.getChild(i);
            if ("call_suffix".equals(child.getType())) {
                TSNode lambda = findFirst(child, "lambda_literal");
                if (lambda != null) return lambda;
            }
        }
        return null;
    }

    private static void collectSimpleIdentifiers(TSNode node, byte[] src, List<String> result) {
        if ("simple_identifier".equals(node.getType())) {
            result.add(text(node, src));
            return;
        }
        for (int i = 0; i < node.getChildCount(); i++) {
            collectSimpleIdentifiers(node.getChild(i), src, result);
        }
    }

    /** Check whether a function subtree contains a bare call to a named local method. */
    private static boolean subtreeContainsLocalCall(TSNode node, String methodName, byte[] src) {
        // call_expression whose function is a simple_identifier with the target name
        if ("call_expression".equals(node.getType())) {
            TSNode fn = node.getChildCount() > 0 ? node.getChild(0) : null;
            if (fn != null && "simple_identifier".equals(fn.getType())
                    && methodName.equals(text(fn, src))) {
                return true;
            }
        }
        for (int i = 0; i < node.getChildCount(); i++) {
            if (subtreeContainsLocalCall(node.getChild(i), methodName, src)) return true;
        }
        return false;
    }

    /** Return the right-hand identifier of a navigation_expression (the selector segment). */
    private static String navigationSelector(TSNode navExpr, byte[] src) {
        // navigation_expression: receiver navigation_suffix
        // navigation_suffix: . simple_identifier
        for (int i = 0; i < navExpr.getChildCount(); i++) {
            TSNode child = navExpr.getChild(i);
            if ("navigation_suffix".equals(child.getType())) {
                return text(findFirstChild(child, "simple_identifier"), src);
            }
        }
        return null;
    }

    // ── Tree traversal helpers ─────────────────────────────────────────────────

    private static TSNode findFirst(TSNode node, String... types) {
        Set<String> typeSet = new HashSet<>(Arrays.asList(types));
        return findFirstMatching(node, typeSet);
    }

    private static TSNode findFirstMatching(TSNode node, Set<String> types) {
        if (node.isNull()) return null;
        if (types.contains(node.getType())) return node;
        for (int i = 0; i < node.getChildCount(); i++) {
            TSNode result = findFirstMatching(node.getChild(i), types);
            if (result != null) return result;
        }
        return null;
    }

    private static TSNode findFirstChild(TSNode node, String type) {
        for (int i = 0; i < node.getChildCount(); i++) {
            TSNode child = node.getChild(i);
            if (type.equals(child.getType())) return child;
        }
        return null;
    }

    private static void forEachChild(TSNode node, String type, Consumer<TSNode> fn) {
        for (int i = 0; i < node.getChildCount(); i++) {
            TSNode child = node.getChild(i);
            if (type.equals(child.getType())) fn.accept(child);
        }
    }

    /**
     * Iterate function_declaration nodes in a body, handling both:
     * - class_body: function_declaration are direct children
     * - lambda_literal (from @Autowired constructor pattern): they are inside statements
     */
    private static void forEachFunction(TSNode body, Consumer<TSNode> fn) {
        for (int i = 0; i < body.getChildCount(); i++) {
            TSNode child = body.getChild(i);
            if ("function_declaration".equals(child.getType())) {
                fn.accept(child);
            } else if ("statements".equals(child.getType())) {
                forEachChild(child, "function_declaration", fn);
            }
        }
    }

    private static String text(TSNode node, byte[] src) {
        if (node == null || node.isNull()) return null;
        int start = node.getStartByte();
        int end = node.getEndByte();
        if (start < 0 || end > src.length || start >= end) return null;
        return new String(src, start, end - start);
    }

    // ── JSON helpers ───────────────────────────────────────────────────────────

    private static String js(String s) {
        return "\"" + s.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n") + "\"";
    }

    private static String jsList(List<String> items) {
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < items.size(); i++) {
            if (i > 0) sb.append(",");
            sb.append(js(items.get(i)));
        }
        return sb.append("]").toString();
    }
}
