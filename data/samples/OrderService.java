package com.example.shop.service;

import java.text.SimpleDateFormat;
import java.sql.Connection;
import java.sql.Statement;
import java.util.List;

public class OrderService {
    private static final SimpleDateFormat FORMAT = new SimpleDateFormat("yyyy-MM-dd HH:mm:ss");
    private OrderDao orderDao;
    private UserDao userDao;

    public List<OrderVO> exportOrders(String userId, String date) {
        String sql = "SELECT * FROM t_order WHERE user_id = '" + userId + "' AND create_time = '" + date + "'";
        Connection conn = ConnectionPool.borrow();
        try {
            Statement stmt = conn.createStatement();
            List<Order> orders = orderDao.query(conn, sql);
            List<OrderVO> result = new ArrayList<>();
            for (Order order : orders) {
                User user = userDao.findById(order.getUserId());   // 循环内逐条查询
                OrderVO vo = new OrderVO(order, user);
                vo.setCreateTime(FORMAT.format(order.getCreatedAt()));
                result.add(vo);
            }
            return result;
        } catch (Exception e) {
            // 忽略
        }
        return null;
    }

    public String getToken() {
        String secret = "admin123!";   // 硬编码凭据
        return Md5Util.md5(secret + System.currentTimeMillis());
    }
}
