package dev.codemap;

import com.github.javaparser.ParserConfiguration;
import com.github.javaparser.StaticJavaParser;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.NodeList;
import com.github.javaparser.ast.body.*;
import com.github.javaparser.ast.expr.*;
import com.github.javaparser.ast.stmt.BlockStmt;
import com.github.javaparser.ast.type.ClassOrInterfaceType;
import com.github.javaparser.ast.type.Type;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.file.Path;
import java.util.*;
import java.util.stream.Collectors;

/**
 * Reads .java file paths from stdin (one per line), outputs JSON array to stdout.
 *
 * Each method now includes:
 *   "callsOnFields": ["dao", "cache"]          // field names (backward compat)
 *   "fieldCalls": [{"field":"dao","type":"DaoType","method":"findById"}]  // rich evidence
 */
public class Scanner {

    private static final Set<String> SKIP_TYPES = Set.of(
        "String","Integer","Long","Boolean","Double","Float","Short","Byte","Character",
        "int","long","boolean","double","float","short","byte","char",
        "List","Map","Set","Collection","Optional","Iterable","Iterator",
        "Object","Class","Enum","Number","Comparable","Serializable",
        "HttpServletRequest","HttpServletResponse","Principal","Locale","TimeZone",
        "ObjectMapper","Logger","Duration","Instant","LocalDate","LocalDateTime",
        "BigDecimal","BigInteger","UUID","URI","URL","InputStream","OutputStream",
        "void","Void","T","R","K","V","E"
    );

    // Skip trivial getter/setter-style method names that add no semantic value
    private static final Set<String> SKIP_METHODS = Set.of(
        "get","set","is","toString","hashCode","equals","clone","build","of","from",
        "stream","iterator","size","isEmpty","contains","add","remove","put","apply"
    );

    public static void main(String[] args) throws Exception {
        ParserConfiguration cfg = new ParserConfiguration();
        cfg.setLanguageLevel(ParserConfiguration.LanguageLevel.JAVA_17);
        StaticJavaParser.setConfiguration(cfg);

        BufferedReader reader = new BufferedReader(new InputStreamReader(System.in));
        StringBuilder out = new StringBuilder("[");
        boolean first = true;
        String line;
        while ((line = reader.readLine()) != null) {
            line = line.strip();
            if (line.isEmpty()) continue;
            String json = scanFile(line);
            if (json == null) continue;
            if (!first) out.append(",");
            out.append(json);
            first = false;
        }
        out.append("]");
        System.out.println(out);
    }

    private static String scanFile(String filePath) {
        try {
            CompilationUnit cu = StaticJavaParser.parse(Path.of(filePath));
            List<String> parts = new ArrayList<>();

            for (TypeDeclaration<?> type : cu.getTypes()) {
                if (!(type instanceof ClassOrInterfaceDeclaration)) continue;
                ClassOrInterfaceDeclaration cls = (ClassOrInterfaceDeclaration) type;
                if (cls.isInterface()) continue;

                String className = cls.getNameAsString();
                List<String> anns = annotations(cls);
                boolean injectFinalFields = anns.contains("RequiredArgsConstructor")
                    || anns.contains("AllArgsConstructor");

                // Collect injected fields: fieldName → typeName
                Map<String, String> fieldTypes = new LinkedHashMap<>();
                for (FieldDeclaration fd : cls.getFields()) {
                    boolean injectedField = fd.getAnnotations().stream()
                        .map(a -> a.getNameAsString())
                        .anyMatch(a -> a.equals("Autowired") || a.equals("Inject"));
                    if (!injectedField && !(injectFinalFields && fd.isFinal())) continue;
                    String typeName = simpleTypeName(fd.getElementType());
                    if (SKIP_TYPES.contains(typeName)) continue;
                    for (VariableDeclarator vd : fd.getVariables()) {
                        fieldTypes.put(vd.getNameAsString(), typeName);
                    }
                }

                // Collect constructor-injected fields
                for (ConstructorDeclaration ctor : cls.getConstructors()) {
                    Map<String, String> paramTypes = new LinkedHashMap<>();
                    for (Parameter param : ctor.getParameters()) {
                        String typeName = simpleTypeName(param.getType());
                        if (!SKIP_TYPES.contains(typeName)) {
                            paramTypes.put(param.getNameAsString(), typeName);
                        }
                    }
                    if (paramTypes.isEmpty()) continue;

                    Set<String> assignedParams = new HashSet<>();
                    for (var stmt : ctor.getBody().getStatements()) {
                        if (stmt.isExpressionStmt()) {
                            Expression expr = stmt.asExpressionStmt().getExpression();
                            if (expr.isAssignExpr()) {
                                AssignExpr ae = expr.asAssignExpr();
                                if (ae.getTarget().isFieldAccessExpr()) {
                                    FieldAccessExpr fa = ae.getTarget().asFieldAccessExpr();
                                    if (fa.getScope().isThisExpr()) {
                                        String fieldName = fa.getNameAsString();
                                        String paramName = ae.getValue().isNameExpr()
                                            ? ae.getValue().asNameExpr().getNameAsString() : null;
                                        if (paramName != null && paramTypes.containsKey(paramName)) {
                                            if (!fieldTypes.containsKey(fieldName)) {
                                                fieldTypes.put(fieldName, paramTypes.get(paramName));
                                            }
                                            assignedParams.add(paramName);
                                        }
                                    }
                                }
                            }
                        }
                    }
                    for (Map.Entry<String, String> e : paramTypes.entrySet()) {
                        if (!assignedParams.contains(e.getKey()) && !fieldTypes.containsKey(e.getKey())) {
                            fieldTypes.put(e.getKey(), e.getValue());
                        }
                    }
                }

                // Collect methods
                List<String> methodJsons = new ArrayList<>();
                for (MethodDeclaration md : cls.getMethods()) {
                    List<String> mAnns = annotations(md);
                    String mappingPath = extractMappingPath(md);

                    // Rich field calls: {field, type, method} deduplicated by field+method
                    List<Map<String, String>> fieldCallsList = new ArrayList<>();
                    Set<String> calledFieldNames = new LinkedHashSet<>();
                    md.getBody().ifPresent(body ->
                        collectFieldCalls(body, fieldTypes, fieldCallsList, calledFieldNames));

                    String fieldCallsJson = fieldCallsList.stream()
                        .map(fc -> String.format("{\"field\":%s,\"type\":%s,\"method\":%s}",
                            jsonStr(fc.get("field")), jsonStr(fc.get("type")), jsonStr(fc.get("method"))))
                        .collect(Collectors.joining(",", "[", "]"));

                    String mJson = String.format(
                        "{\"name\":%s,\"annotations\":%s,\"mappingPath\":%s,\"callsOnFields\":%s,\"fieldCalls\":%s}",
                        jsonStr(md.getNameAsString()),
                        jsonStrList(mAnns),
                        mappingPath != null ? jsonStr(mappingPath) : "null",
                        jsonStrList(new ArrayList<>(calledFieldNames)),
                        fieldCallsJson
                    );
                    methodJsons.add(mJson);
                }

                // Build fields JSON
                List<String> fieldJsons = new ArrayList<>();
                for (Map.Entry<String, String> e : fieldTypes.entrySet()) {
                    fieldJsons.add(String.format("{\"name\":%s,\"type\":%s}",
                        jsonStr(e.getKey()), jsonStr(e.getValue())));
                }

                String classJson = String.format(
                    "{\"file\":%s,\"className\":%s,\"annotations\":%s,\"fields\":[%s],\"methods\":[%s]}",
                    jsonStr(filePath),
                    jsonStr(className),
                    jsonStrList(anns),
                    String.join(",", fieldJsons),
                    String.join(",", methodJsons)
                );
                parts.add(classJson);
            }
            return parts.isEmpty() ? null : parts.get(0);
        } catch (Exception e) {
            System.err.println("WARN: could not parse " + filePath + ": " + e.getMessage());
            return null;
        }
    }

    /**
     * Collect field.method() calls. Populates:
     *   fieldCallsList — rich {field, type, method} entries (deduplicated by field+method)
     *   calledFieldNames — just field names (for backward compat callsOnFields)
     */
    private static void collectFieldCalls(BlockStmt block, Map<String, String> fieldTypes,
                                          List<Map<String, String>> fieldCallsList,
                                          Set<String> calledFieldNames) {
        Set<String> seen = new LinkedHashSet<>(); // dedup key: field\0method
        block.findAll(MethodCallExpr.class).forEach(call -> {
            call.getScope().ifPresent(scope -> {
                String receiver = directReceiver(scope);
                if (receiver == null || !fieldTypes.containsKey(receiver)) return;
                String methodName = call.getNameAsString();
                if (SKIP_METHODS.contains(methodName)) return;
                calledFieldNames.add(receiver);
                String key = receiver + "\0" + methodName;
                if (seen.add(key)) {
                    Map<String, String> fc = new LinkedHashMap<>();
                    fc.put("field", receiver);
                    fc.put("type", fieldTypes.get(receiver));
                    fc.put("method", methodName);
                    fieldCallsList.add(fc);
                }
            });
        });
    }

    /** Returns the direct receiver name only for simple and this.field patterns. */
    private static String directReceiver(Expression expr) {
        if (expr.isNameExpr()) return expr.asNameExpr().getNameAsString();
        if (expr.isFieldAccessExpr()) {
            FieldAccessExpr fa = expr.asFieldAccessExpr();
            if (fa.getScope().isThisExpr()) return fa.getNameAsString();
        }
        return null;
    }

    private static List<String> annotations(BodyDeclaration<?> node) {
        return node.getAnnotations().stream()
            .map(a -> a.getNameAsString())
            .collect(Collectors.toList());
    }

    private static String extractMappingPath(MethodDeclaration md) {
        Set<String> mappingAnns = Set.of(
            "GetMapping","PostMapping","PutMapping","DeleteMapping","PatchMapping","RequestMapping"
        );
        for (AnnotationExpr ann : md.getAnnotations()) {
            if (!mappingAnns.contains(ann.getNameAsString())) continue;
            if (ann.isSingleMemberAnnotationExpr()) {
                Expression val = ann.asSingleMemberAnnotationExpr().getMemberValue();
                if (val.isStringLiteralExpr()) return val.asStringLiteralExpr().asString();
            } else if (ann.isNormalAnnotationExpr()) {
                for (MemberValuePair pair : ann.asNormalAnnotationExpr().getPairs()) {
                    if (pair.getNameAsString().equals("value") || pair.getNameAsString().equals("path")) {
                        Expression val = pair.getValue();
                        if (val.isStringLiteralExpr()) return val.asStringLiteralExpr().asString();
                        if (val.isArrayInitializerExpr()) {
                            NodeList<Expression> vals = val.asArrayInitializerExpr().getValues();
                            if (!vals.isEmpty() && vals.get(0).isStringLiteralExpr()) {
                                return vals.get(0).asStringLiteralExpr().asString();
                            }
                        }
                    }
                }
            }
        }
        return null;
    }

    private static String simpleTypeName(Type type) {
        if (type.isClassOrInterfaceType()) {
            ClassOrInterfaceType ct = type.asClassOrInterfaceType();
            String name = ct.getNameAsString();
            int dot = name.lastIndexOf('.');
            return dot >= 0 ? name.substring(dot + 1) : name;
        }
        return type.asString();
    }

    private static String jsonStr(String s) {
        if (s == null) return "null";
        return "\"" + s.replace("\\", "\\\\").replace("\"", "\\\"")
            .replace("\n", "\\n").replace("\r", "\\r") + "\"";
    }

    private static String jsonStrList(List<String> list) {
        return "[" + list.stream().map(Scanner::jsonStr).collect(Collectors.joining(",")) + "]";
    }
}
