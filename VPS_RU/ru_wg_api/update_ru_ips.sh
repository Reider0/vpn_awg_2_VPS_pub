#!/bin/bash

echo "🔄 Downloading latest RU ASN subnets..."

echo "create ru_nets hash:net -exist" > /tmp/ipset.txt
echo "flush ru_nets" >> /tmp/ipset.txt

# Используем надежный репозиторий, фильтруем только валидные IP-подсети, игнорируем HTML-ошибки
curl -sSfL "https://raw.githubusercontent.com/dvershinin/ip-country-cidr/master/ru-ipv4.cidr" | grep -E '^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}/[0-9]{1,2}' | sed 's/^/add ru_nets /' >> /tmp/ipset.txt

# Быстро загружаем всё в память ядра
ipset restore < /tmp/ipset.txt

echo "✅ RU subnets successfully updated in 'ru_nets'."