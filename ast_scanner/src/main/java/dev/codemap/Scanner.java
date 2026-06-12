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
 * Each element:
 * {
 *   "file": "...",
 *   "className": "...",
 *   "annotations": ["Service", ...],
 *   "fields": [{"name":"dao","type":"PickupPointDao"}, ...],
 *   "methods": [
 *     {
 *       "name": "handleFoo",
 *       "annotations": ["GetMapping"],
 *       "mappingPath": "/api/foo",
 *       "callsOnFields": ["dao", "cache"]   // field names referenced in body
 *     }
 *   ]
 * }
 */
public class Scanner {

    // Primitive / JDK types we don't care about as dependencies
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

                // Collect fields declared in the class body
                Map<String, String> fieldTypes = new LinkedHashMap<>();
                for (FieldDeclaration fd : cls.getFields()) {
                    String typeName = simpleTypeName(fd.getElementType());
                    if (SKIP_TYPES.contains(typeName)) continue;
                    for (VariableDeclarator vd : fd.getVariables()) {
                        fieldTypes.put(vd.getNameAsString(), typeName);
                    }
                }

                // Collect constructor params and this.field = param assignments
                for (ConstructorDeclaration ctor : cls.getConstructors()) {
                    Map<String, String> paramTypes = new LinkedHashMap<>();
                    for (Parameter param : ctor.getParameters()) {
                        String typeName = simpleTypeName(param.getType());
                        if (!SKIP_TYPES.contains(typeName)) {
                            paramTypes.put(param.getNameAsString(), typeName);
                        }
                    }
                    if (paramTypes.isEmpty()) continue;

                    // Map this.field = param → use field name as key
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
                    // Params not assigned to this.field: use param name directly
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

                    // Find all field names called in this method body
                    Set<String> calledFields = new LinkedHashSet<>();
                    md.getBody().ifPresent(body ->
                        collectFieldCalls(body, fieldTypes.keySet(), calledFields));

                    String mJson = String.format(
                        "{\"name\":%s,\"annotations\":%s,\"mappingPath\":%s,\"callsOnFields\":%s}",
                        jsonStr(md.getNameAsString()),
                        jsonStrList(mAnns),
                        mappingPath != null ? jsonStr(mappingPath) : "null",
                        jsonStrList(new ArrayList<>(calledFields))
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
            // Return null on parse error — Python falls back to regex
            System.err.println("WARN: could not parse " + filePath + ": " + e.getMessage());
            return null;
        }
    }

    /** Collect calls of the form fieldName.method() or fieldName.field within a block. */
    private static void collectFieldCalls(BlockStmt block, Set<String> fieldNames, Set<String> out) {
        block.findAll(MethodCallExpr.class).forEach(call -> {
            call.getScope().ifPresent(scope -> {
                String receiver = receiverName(scope);
                if (receiver != null && fieldNames.contains(receiver)) {
                    out.add(receiver);
                }
            });
        });
        // Also field access: fieldName.something (e.g. cache.get, dao.query)
        block.findAll(FieldAccessExpr.class).forEach(fa -> {
            String receiver = receiverName(fa.getScope());
            if (receiver != null && fieldNames.contains(receiver)) {
                out.add(receiver);
            }
        });
    }

    private static String receiverName(Expression expr) {
        if (expr.isNameExpr()) return expr.asNameExpr().getNameAsString();
        if (expr.isThisExpr()) return null;
        if (expr.isMethodCallExpr()) {
            // chain: this.foo.bar() → scope is this.foo, which may resolve to a field
            return expr.asMethodCallExpr().getScope()
                .map(Scanner::receiverName).orElse(null);
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
            // Strip generics, take simple name (last segment of qualified name)
            ClassOrInterfaceType ct = type.asClassOrInterfaceType();
            String name = ct.getNameAsString();
            // Handle fully qualified: com.example.Foo → Foo
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
