import React from 'react';
import ReactDOM from 'react-dom/client';
import { XProvider } from '@ant-design/x';
import zhCNX from '@ant-design/x/locale/zh_CN';
import zhCN from 'antd/locale/zh_CN';
import 'antd/dist/reset.css';
import 'highlight.js/styles/github-dark.css';

import RuntimeApp from './App.jsx';
import './styles/app.css';

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <XProvider locale={{ ...zhCNX, ...zhCN }}>
      <RuntimeApp />
    </XProvider>
  </React.StrictMode>,
);
