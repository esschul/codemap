package com.example.order

import org.springframework.beans.factory.annotation.Autowired
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.RequestMapping
import org.springframework.web.bind.annotation.RestController

/**
 * Regression fixture: class-level annotation with ::class argument.
 * @Schema(implementation = Foo::class) contains "class" as a token inside
 * value_arguments. The multi-annotation fallback must NOT be triggered by
 * this "class" keyword — it should only fire when "class" appears in the
 * infix_expression that represents the misparsed class declaration.
 */
@RestController
@RequestMapping("/schema")
internal class SchemaAnnotatedController
@Autowired constructor(
    private val orderService: OrderService,
) {
    @GetMapping("/orders")
    fun list(): List<Any> = orderService.findAll()
}
