package com.example;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/orders")
public class OrderController {
    private final OrderService orderService;

    public OrderController(OrderService orderService) {
        this.orderService = orderService;
    }

    @GetMapping("/{id}")
    public Object getOrder(@PathVariable Long id) {
        return orderService.findOrder(id);
    }

    @PostMapping
    public Object createOrder(@RequestBody Object body) {
        return orderService.createOrder(body);
    }
}
