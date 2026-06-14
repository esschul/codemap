package com.example.order

import org.springframework.beans.factory.annotation.Autowired
import org.springframework.context.annotation.DependsOn
import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.RequestMapping
import org.springframework.web.bind.annotation.RestController

/**
 * Regression fixture: 3+ class-level annotations before "internal class" + annotated constructor.
 * tree-sitter 0.3.x misparses this as prefix_expression chain instead of class_declaration.
 * KtScanner must fall back to extracting class name and annotations from the misparsed AST.
 */
@RestController
@DependsOn("requestBuilder")
@RequestMapping(value = ["/api", "/"])
internal class MultiAnnotationController
@Autowired constructor(
    private val orderService: OrderService,
    private val inventoryClient: InventoryClient,
) {
    @GetMapping("/multi/orders")
    fun getOrders(): List<Any> = orderService.findAll()

    @GetMapping("/multi/inventory")
    fun checkInventory(): Any = inventoryClient.getStock()
}
